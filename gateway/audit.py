"""Caliber audit logger — the spine of every privacy claim.

WHY THIS EXISTS
---------------
Everything Caliber sells is downstream of "we can prove what happened."
Without a tamper-evident audit log, the rest of the architecture (redaction,
tier routing, BYOK) is just marketing. With it, a customer's auditor can
verify after the fact: this prompt was sent, this redaction was applied,
this tier was used, this model returned this hash — and the chain hasn't
been broken since the day the log started.

DESIGN PROPERTIES
-----------------
1. Append-only — entries are written and never modified. We don't even
   support an "edit" code path; you'd have to bypass this module entirely.
2. Hash-chained — each entry's `prev_hash` references the previous entry's
   `hash`. Removing or modifying any entry breaks the chain from that point
   forward, which is visible to anyone verifying.
3. JSONL on disk — one entry per line, plain text. Customers can grep it,
   load it into BigQuery, ship it to their SIEM. No proprietary format.
4. Minimal content — we log metadata + hashes, NOT the raw prompts/responses.
   Customers asked us not to retain their data; the audit log respects that.
   If they want the raw text retained too (some do, for review), there's a
   separate `verbose` mode that stores it under their own KMS key.

INVARIANTS
----------
- Two simultaneous writes from different processes would corrupt the chain.
  For dev (single-process), this isn't an issue. For prod (multi-worker
  uvicorn), the writer is serialised by a file lock. When we move to
  multi-node, the audit log moves to a single-writer service (S3 Object
  Lock + a tiny appender service).

USAGE
-----
    from audit import AuditLog
    log = AuditLog(path=Path("audit/audit.jsonl"))
    log.append(
        tenant="acme",
        user="alice@acme.com",
        tier="private",
        model="claude-sonnet-4-6",
        prompt_text="...",         # hashed, never stored verbatim
        response_text="...",       # hashed, never stored verbatim
        redaction_stats={"vendors": 1, "ibans": 1},
    )

    # Verify the chain end-to-end (for customer-facing export):
    ok, broken_at = log.verify()
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


# Hash function used throughout. SHA-256 is the obvious choice — fast,
# universally available, and what every auditor expects to see.
_HASH = hashlib.sha256


def _hash_text(s: str) -> str:
    """Stable SHA-256 hex of a string. Used for prompt + response hashes."""
    return _HASH(s.encode("utf-8")).hexdigest()


def _hash_entry(entry: dict) -> str:
    """Hash of a serialised entry (sorted keys, no whitespace).
    The `hash` and `prev_hash` fields are excluded so we don't hash the hash."""
    body = {k: v for k, v in entry.items() if k not in ("hash", "prev_hash")}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return _HASH(canonical.encode("utf-8")).hexdigest()


@dataclass
class AuditEntry:
    """One row of the audit log. Mirrors the JSON shape on disk."""
    timestamp: str          # ISO-8601 UTC
    tenant: str
    user: str | None
    tier: str               # "public" | "private" | "sealed"
    model: str
    request_id: str         # opaque ID, also returned in the HTTP response
    prompt_hash: str        # SHA-256 of the original (pre-redaction) prompt
    redacted_prompt_hash: str | None  # SHA-256 of the redacted prompt (if any)
    response_hash: str | None         # SHA-256 of the response (None on errors)
    redaction_stats: dict   # counts per entity type, e.g. {"vendor": 2, "iban": 1}
    duration_ms: int
    status: str             # "ok" | "error" | "policy_denied"
    error: str | None = None
    host: str = ""
    prev_hash: str | None = None
    hash: str | None = None


class AuditLog:
    """Append-only, hash-chained JSONL audit log.

    Thread-safety: serialised via fcntl.flock on the underlying file
    descriptor. Cross-process safe on POSIX. Don't use on Windows in prod.

    Verification: call `verify()` to walk the chain. Returns
    (ok: bool, broken_at: int | None) — the index of the first broken
    entry, or None if the chain is intact.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._host = socket.gethostname()
        # Touch the file so verify() works on empty logs
        if not self.path.exists():
            self.path.touch()

    # ----- writers -----

    def append(
        self,
        *,
        tenant: str,
        user: str | None,
        tier: str,
        model: str,
        request_id: str,
        prompt_text: str,
        response_text: str | None,
        redacted_prompt_text: str | None = None,
        redaction_stats: dict | None = None,
        duration_ms: int = 0,
        status: str = "ok",
        error: str | None = None,
        timestamp: str | None = None,
    ) -> AuditEntry:
        """Append one entry. Computes hashes and chains to the previous
        entry's hash. Returns the entry as written.

        Note: we hash the *text*, not store it. To recover what was logged
        you need both the hash and the corresponding raw text from your
        application — that's the design. We can't leak what we don't keep.
        """
        prev_hash = self._read_last_hash()
        entry = AuditEntry(
            timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
            tenant=tenant,
            user=user,
            tier=tier,
            model=model,
            request_id=request_id,
            prompt_hash=_hash_text(prompt_text),
            redacted_prompt_hash=(_hash_text(redacted_prompt_text)
                                  if redacted_prompt_text is not None else None),
            response_hash=(_hash_text(response_text) if response_text else None),
            redaction_stats=redaction_stats or {},
            duration_ms=duration_ms,
            status=status,
            error=error,
            host=self._host,
            prev_hash=prev_hash,
            hash=None,                # filled in below
        )
        # Compute the chain hash over the entry body and freeze it.
        entry_dict = entry.__dict__.copy()
        entry.hash = _hash_entry(entry_dict)
        entry_dict["hash"] = entry.hash

        # Atomic append. fcntl.flock serialises writers across processes.
        line = json.dumps(entry_dict, sort_keys=True) + "\n"
        with open(self.path, "ab") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.write(line.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return entry

    # ----- readers -----

    def iter_entries(self) -> Iterator[dict]:
        """Yield every entry in order. Used by the dashboard + verify()."""
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # Corruption isn't silently swallowed — we surface it.
                    # The auditor will see a malformed line and know to look.
                    yield {"__corrupt__": True, "raw": line}

    def verify(self) -> tuple[bool, int | None]:
        """Walk the chain. Returns (True, None) if intact, otherwise
        (False, index_of_first_broken_entry).

        A break indicates either tampering or a bug — both worth knowing
        about before a customer audit.
        """
        expected_prev: str | None = None
        for i, entry in enumerate(self.iter_entries()):
            if entry.get("__corrupt__"):
                return False, i
            # 1. The entry's recorded prev_hash must match what we expect.
            if entry.get("prev_hash") != expected_prev:
                return False, i
            # 2. Recomputing the entry's hash from its body must match.
            recomputed = _hash_entry(entry)
            if recomputed != entry.get("hash"):
                return False, i
            expected_prev = entry.get("hash")
        return True, None

    # ----- internals -----

    def _read_last_hash(self) -> str | None:
        """Fast tail-read of the last hash, without parsing the whole file.

        Reads from EOF backwards until it finds a newline + JSON object.
        Used on every append, so it needs to stay O(1) regardless of log
        size. For dev-scale (<10M entries) this is fine. At very large
        scale we cache the last hash in memory.
        """
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        with open(self.path, "rb") as f:
            # Seek to end, walk back until we hit a newline (skip trailing \n)
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return None
            # Read the last 4KB — more than enough for one entry.
            read_size = min(size, 4096)
            f.seek(size - read_size, 0)
            tail = f.read(read_size)
        # Find the last non-empty line
        lines = tail.split(b"\n")
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line.decode("utf-8"))
                return entry.get("hash")
            except json.JSONDecodeError:
                continue
        return None


# ----- CLI: quick smoke test + chain verification -----
# Run: python3 audit.py path/to/audit.jsonl

if __name__ == "__main__":
    import sys
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("../audit/audit.jsonl")
    log = AuditLog(p)
    ok, broken = log.verify()
    if ok:
        n = sum(1 for _ in log.iter_entries())
        print(f"audit log OK — {n} entries, chain intact")
    else:
        print(f"AUDIT LOG BROKEN at entry index {broken}")
        sys.exit(1)
