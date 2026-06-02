"""Seed Caliber's audit log with realistic-looking demo data.

WHY
---
An empty dashboard looks like vapor. A dashboard with several days of
believable history — varied tenants, realistic prompts, mixed tiers — looks
like a product that's been in production. This script generates that
history.

WHAT IT DOES
------------
1. (Optionally) clears the existing audit log.
2. Walks chronologically through the last N days.
3. For each simulated call, builds an Anthropic-shaped prompt, runs it
   through the REAL redactor (so the redaction stats are accurate), picks
   a realistic latency, and writes an audit entry via the REAL AuditLog.

Because we use the real AuditLog, the hash chain validates end-to-end —
the same `verify()` call any prospect can run will return ok=True. There
are no faked rows.

RUN
---
    # Clear and re-seed:
    python3 scripts/seed_demo_data.py --reset

    # Add more entries on top of whatever's already there:
    python3 scripts/seed_demo_data.py

    # Custom: 200 entries across 14 days:
    python3 scripts/seed_demo_data.py --reset --entries 200 --days 14

SAFE TO RE-RUN
--------------
Without --reset, entries are appended. The new entries chain on top of
existing ones cleanly. The dashboard's KPI tiles will simply show more.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent.resolve()
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "gateway"))

from audit import AuditLog
from redactor import Redactor


# --------------------------------------------------------------------------
# Demo content — finance-flavored prompts that exercise different entities.
# Each tuple: (prompt_text, response_template)
# The response template is short — we don't store the body, only its hash,
# so this just gives the audit log realistic response_hash values.
# --------------------------------------------------------------------------

PROMPTS_PUBLIC: list[tuple[str, str]] = [
    ("Explain envelope encryption in two short sentences.",
     "Envelope encryption uses a data key to encrypt content, then encrypts the data key with a master key…"),
    ("What's the difference between BYOK and HYOK?",
     "BYOK (Bring Your Own Key) means the customer provides the key but you hold it; HYOK (Hold Your Own Key) means the customer holds it…"),
    ("Draft a one-paragraph summary of SOC 2 Type II for a non-technical audience.",
     "SOC 2 Type II is an external audit that verifies a service provider's controls operate effectively over a period of time…"),
    ("Write a SQL query to find duplicate rows by (vendor, amount).",
     "SELECT vendor, amount, COUNT(*) FROM invoices GROUP BY vendor, amount HAVING COUNT(*) > 1;"),
    ("Generate a Python regex for a Swiss IBAN.",
     "^CH\\d{2}\\s?(?:\\d{4}\\s?){4}\\d{1}$"),
    ("Translate this German phrase: 'Zahlungsbedingungen: 30 Tage netto'",
     "'Payment terms: net 30 days' — a vendor invoice phrase indicating payment is due 30 days after invoice date."),
    ("How would you structure a fund accounting double-entry for a management fee accrual?",
     "Dr Management Fee Expense; Cr Accrued Management Fee Payable. Reversed when the fee is paid…"),
    ("Suggest unit-test cases for a number parser that accepts Swiss formats.",
     "Test cases: '1234' → 1234, '1\\'234.50' → 1234.5, '1.234,50' (German) → 1234.5, empty → None, malformed → raise…"),
    ("What's the typical hurdle rate convention in a hedge fund 2/20 structure?",
     "Most funds use either 0% (no hurdle, performance fee applies to all gains) or a benchmark-linked rate…"),
    ("Explain why ZDR contracts matter for using public LLMs with customer data.",
     "Zero-Data-Retention (ZDR) contracts commit the provider to not store prompts/responses beyond a short transient window…"),
]

PROMPTS_PRIVATE: list[tuple[str, str]] = [
    ("Summarise the payment terms in this BerglandRealtyAG invoice for CHF 21090.30 charged to Meridian Alpha AG.",
     "Invoice from BerglandRealtyAG (CHF 21,090.30) charged to Meridian Alpha AG. Payment terms: net 30…"),
    ("Categorise this BookKeep AG charge for CHF 1631.50 — operating expense or contractor?",
     "Likely operating expense (bookkeeping services). Maps to G&A → Professional services…"),
    ("Why might Carlos Eduardo Mendes Silva appear on multiple payment runs?",
     "Recurring vendor, possibly contractor. Check the contract terms and payment frequency…"),
    ("Draft an email to AusgleichskasseZug confirming the CHF 4423.09 SVA payment for May.",
     "Subject: SVA payment confirmation — May 2026. Body: We confirm CHF 4,423.09 paid on…"),
    ("Reconcile this Wise transfer of EUR 14513 from Meridian Studio AG against the AP register.",
     "EUR 14,513 from Meridian Studio AG matches invoice INV-2026-0408 from supplier. Status: paid, no discrepancy…"),
    ("Why is the invoice from BerglandRealtyAG dated 2026-05-11 but executed 2026-05-16?",
     "5-day lag is typical — invoice issued mid-month, payment cleared after Sygnum's processing cycle. No anomaly…"),
    ("Compare these two vendor names: 'BerglandRealtyAG' and 'Bergland Realty AG' — are they the same entity?",
     "Yes — same vendor, with stripped umlauts and spaces. Common artifact of OCR or German keyboard normalisation…"),
    ("List the top 5 expense categories for Meridian Alpha AG this quarter.",
     "1. Payroll · 2. Rent · 3. Professional services · 4. Software · 5. Travel & entertainment…"),
    ("Estimate when invoice INV-2026-0408 will likely be paid based on its due date.",
     "Due 2026-08-31; payment date typically clears 0-3 days before due. Expected: 2026-08-28 to 2026-08-31…"),
    ("Suggest a JSON schema for normalising vendor names across our AP register.",
     "{ \"canonical_name\": \"...\", \"aliases\": [], \"vat_id\": \"...\", \"country\": \"...\", \"first_seen\": \"YYYY-MM-DD\" }…"),
]

PROMPTS_SEALED: list[tuple[str, str]] = [
    ("Reconcile Sygnum txn zv20260521/192940/1 against debit account 84.008.505.147.1 (IBAN CH51 0700 0000 0000 0000 0).",
     "Sygnum txn zv20260521/192940/1 debited CHF 12,982.15 from account 84.008.505.147.1 on 2026-05-22…"),
    ("Trace the cash flow path: account 84.009.396.036.1 → IBAN CH51 0700 0000 0000 0000 0 → vendor.",
     "Path: Meridian Studio AG CHF account → external IBAN → vendor 'lukas berger'. Settlement: zv20260521/192940/1…"),
    ("Why does account 84.008.505.147.1 show outflow of CHF 17540.66 scheduled for 2026-06-12?",
     "Scheduled June payroll run — 9 employees, total CHF 17,540.66. Standard monthly batch…"),
    ("Cross-check IBAN CH51 0700 0000 0000 0000 0 against our supplier whitelist.",
     "IBAN found in supplier whitelist — vendor 'BerglandRealtyAG' (rent payments). No anomaly. Last paid 2026-05-15…"),
    ("List every transaction debiting 84.008.146.143.8 since 2026-05-01.",
     "Account 84.008.146.143.8 (1of1 AG USD) — 3 transactions since 2026-05-01: $82.41 fee, $0 sweep, $1,200 contractor…"),
    ("Match this stuck payment (CHF 8780.55, exec 2026-05-06, account 84.008.505.147.1) to a Sygnum txn ref if it cleared.",
     "No matching zv ref found in settled history — payment is still in '1 authorisation open' state. Needs second signature…"),
    ("Flag any 2026 transactions from account 84.000.463.467.6 above CHF 50,000.",
     "Account 84.000.463.467.6 (Meridian AG CHF) — 2 transactions above CHF 50,000 YTD: CHF 67,200 (vendor settlement), CHF 89,400 (capital transfer)…"),
    ("Identify intercompany flows: Meridian Studio AG → Meridian Alpha AG, last 30 days.",
     "3 IC transfers found: CHF 12,000 (2026-05-03), CHF 8,500 (2026-05-15), CHF 21,000 (2026-05-22). Total CHF 41,500…"),
]

# Mix of demo tenants for the multi-tenant feel
TENANTS = [
    ("meridian",     "amaan@meridian.com"),
    ("meridian",     "sue@meridian.com"),
    ("meridian",     "ryan@meridian.com"),
    ("acme",          "cfo@acme.example"),
    ("acme",          "controller@acme.example"),
    ("helvetica",     "treasurer@helvetica.example"),
    ("glacier",       "ops@glacier.example"),
]

# Models seen in production — gives the audit log realistic variety
MODELS_PUBLIC  = ["claude-sonnet-4-6", "claude-opus-4-6", "gpt-4-turbo"]
MODELS_PRIVATE = ["claude-sonnet-4-6", "claude-opus-4-6"]
MODELS_SEALED  = ["llama-3.3-70b-instruct", "llama-3.3-70b-instruct", "qwen-2.5-72b-instruct"]

# Latency profiles (ms) — local model is slower than API
LATENCY_PUBLIC  = (700, 1900)
LATENCY_PRIVATE = (1100, 2400)
LATENCY_SEALED  = (1500, 3200)


def pick(seq):
    return random.choice(seq)


def make_call(redactor: Redactor, kind: str, when: datetime):
    """Build one fake call's worth of arguments to feed AuditLog.append()."""
    if kind == "public":
        prompt, response = pick(PROMPTS_PUBLIC)
        latency = random.randint(*LATENCY_PUBLIC)
        model   = pick(MODELS_PUBLIC)
        tier    = "public"
        redacted_text = None
    elif kind == "private":
        prompt, response = pick(PROMPTS_PRIVATE)
        latency = random.randint(*LATENCY_PRIVATE)
        model   = pick(MODELS_PRIVATE)
        tier    = "private"
        redacted_text = redactor.redact(prompt).redacted_text
    else:  # sealed
        prompt, response = pick(PROMPTS_SEALED)
        latency = random.randint(*LATENCY_SEALED)
        model   = pick(MODELS_SEALED)
        tier    = "sealed"
        redacted_text = None  # sealed doesn't redact — local model sees it all

    stats = redactor.redact(prompt).stats
    tenant, user = pick(TENANTS)

    # Realistic failure rate — ~3% errors so the dashboard shows the
    # "even failures get audited" story
    failed = random.random() < 0.03
    status = "error" if failed else "ok"
    error  = "upstream timeout (demo)" if failed else None
    response_text = None if failed else response

    return {
        "tenant":         tenant,
        "user":           user,
        "tier":           tier,
        "model":          model,
        "request_id":     f"req_{uuid.uuid4().hex[:16]}",
        "prompt_text":    prompt,
        "response_text":  response_text,
        "redacted_prompt_text": redacted_text,
        "redaction_stats": stats,
        "duration_ms":    latency,
        "status":         status,
        "error":          error,
        "timestamp":      when.isoformat(),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true",
                   help="Delete existing audit.jsonl before seeding")
    p.add_argument("--entries", type=int, default=60,
                   help="How many entries to generate (default: 60)")
    p.add_argument("--days", type=int, default=4,
                   help="Spread across last N days (default: 4)")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed for reproducible output (default: 42)")
    args = p.parse_args()

    random.seed(args.seed)

    audit_path = ROOT / "audit" / "audit.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    if args.reset and audit_path.exists():
        # Archive what was there in case the user wants to roll back
        backup = audit_path.with_suffix(f".jsonl.bak.{int(datetime.now().timestamp())}")
        audit_path.rename(backup)
        print(f"→ archived previous audit log to: {backup.name}")

    log = AuditLog(audit_path)
    redactor = Redactor()

    # Build a sequence of timestamps that:
    #   - covers the last `days` days
    #   - concentrates ~50% of activity on "today" (since that's what the
    #     KPI tile shows)
    #   - has light gaps (real systems don't get a request every minute)
    now = datetime.now(timezone.utc)
    timestamps: list[datetime] = []

    today_share = int(args.entries * 0.50)
    rest        = args.entries - today_share

    # Today: cluster between 09:00 UTC and now
    today_start = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if today_start > now:
        today_start = now - timedelta(hours=8)
    for _ in range(today_share):
        delta = (now - today_start).total_seconds() * random.random()
        timestamps.append(today_start + timedelta(seconds=delta))

    # Previous days: spread across business hours
    for i in range(rest):
        days_ago = random.randint(1, args.days - 1)
        day = (now - timedelta(days=days_ago)).replace(microsecond=0)
        # Business hours, UTC-ish
        h = random.randint(7, 18)
        m = random.randint(0, 59)
        s = random.randint(0, 59)
        timestamps.append(day.replace(hour=h, minute=m, second=s))

    timestamps.sort()   # chronological so the chain reflects real ordering

    # Tier distribution — gives the dashboard a believable mix
    # 40% public, 35% private, 25% sealed
    tier_pool = (["public"] * 40) + (["private"] * 35) + (["sealed"] * 25)

    print(f"→ seeding {len(timestamps)} entries across {args.days} days "
          f"(today={today_share}, prior={rest})")
    written = 0
    for ts in timestamps:
        kind = random.choice(tier_pool)
        call = make_call(redactor, kind, ts)
        log.append(**call)
        written += 1

    # Verify the chain end-to-end so we can sell it confidently
    ok, broken = log.verify()
    print(f"→ wrote {written} entries · chain verify: ok={ok} broken_at={broken}")
    print(f"→ audit log: {audit_path}")

    # Print a tier breakdown
    from collections import Counter
    by_tier = Counter()
    for e in log.iter_entries():
        by_tier[e.get("tier", "?")] += 1
    for tier, n in sorted(by_tier.items(), key=lambda x: -x[1]):
        print(f"    {tier:8s} {n}")


if __name__ == "__main__":
    main()
