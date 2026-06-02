"""Caliber redactor — strip sensitive entities before anything leaves
the trust boundary.

WHY THIS EXISTS
---------------
"Customers' data never reaches Anthropic / OpenAI in identifiable form" is
the single most-asked privacy guarantee. This module is where that guarantee
is *enforced*. If the redactor misses an entity, the privacy claim is
hollow. Treat changes here with the same caution you'd treat changes to a
crypto library.

HOW IT WORKS
------------
For each request:

  1. Detect entities in the prompt — names, emails, IBANs, account numbers,
     credit cards, dates of birth, plus finance-domain entities we add
     ourselves (vendor names, invoice IDs, Swiss bank account formats).
  2. Replace each detection with a stable token of the form
     `[TYPE_NNNN]` — e.g. `[VENDOR_0001]`, `[IBAN_0002]`. The mapping
     (token → original value) is kept in a per-request rehydration map
     that NEVER leaves the gateway process.
  3. The redacted prompt goes to the LLM provider.
  4. On the way back, we rehydrate — same tokens in the response get
     replaced with original values. The customer sees their real data;
     Anthropic only ever saw tokens.

TWO BACKENDS
------------
1. Presidio (Microsoft's open-source PII detector). Heavy install (~500MB
   with spaCy NLP model) but production-quality. Recommended for any
   non-demo deployment.
2. Regex-only fallback. Built-in, no dependencies. Handles the obvious
   stuff — emails, IBANs, credit cards, Swiss account numbers — but misses
   names and free-form vendor mentions. Useful for first-run demos where
   you don't want to install spaCy yet.

The redactor auto-detects which is available at import time and tells the
caller via `RedactorBackend.NAME`. The gateway's `/health` endpoint surfaces
this so you can see at-a-glance whether the demo is in degraded mode.

EXTENDING
---------
Custom recognisers (finance-specific entities) live in `_FINANCE_PATTERNS`
and `_TENANT_RECOGNISERS`. Add a new pattern there. Don't sprinkle regex
matches elsewhere in the codebase — the redactor is the single place that
decides what's sensitive.

TESTING
-------
Adversarial test cases live in tests/test_redactor.py. Every new pattern
gets a test case showing what it catches AND a test case showing what it
deliberately doesn't catch (so a future change doesn't accidentally widen
the net and break customer prompts).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Iterable


# --------------------------------------------------------------------------
# Finance-domain entity patterns
#
# These run REGARDLESS of which backend is selected. They cover entities
# Presidio doesn't handle well out of the box: Swiss bank account formats,
# Sygnum txn refs, internal vendor codes, etc. Add new patterns here.
# --------------------------------------------------------------------------

_FINANCE_PATTERNS: dict[str, re.Pattern] = {
    # Swiss bank account format: "84.008.505.147.1" (Sygnum-style)
    # Matches: 2 digits . 3 digits . 3 digits . 3 digits . 1 digit
    "SWISS_ACCT": re.compile(r"\b\d{2}\.\d{3}\.\d{3}\.\d{3}\.\d\b"),
    # Sygnum payment refs: "zv20260521/192940/1"
    "SYGNUM_REF": re.compile(r"\bzv\d{8}/\d{6}/\d+\b", re.IGNORECASE),
    # IBAN — generic, covers all countries
    "IBAN": re.compile(r"\b[A-Z]{2}\d{2}(?:\s?\w{4}){2,7}\s?\w{1,4}\b"),
    # Credit-card-like 13–19 digit groups (Luhn check would be more precise;
    # this is permissive on purpose — false positives are safer than misses)
    "CC": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    # Email (RFC-pragma compliant, not strictly RFC 5322)
    "EMAIL": re.compile(r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
    # CHF/USD/EUR amounts above a threshold worth flagging (large invoices)
    # Doesn't redact small amounts to avoid noise on demo content.
    "AMOUNT": re.compile(r"\b(?:CHF|USD|EUR|GBP)\s?\d{4,}(?:[.,]\d{2})?\b"),
}

# Generic patterns that look "vendor-like" — capitalised multi-word names
# followed by AG/GmbH/SA/Ltd/Inc. Used by the regex backend (Presidio's NER
# does a better job, but this catches the obvious cases without spaCy).
_VENDOR_LIKE_RE = re.compile(
    r"\b(?:[A-Z][a-zA-Z0-9]{1,}\s){0,4}[A-Z][a-zA-Z0-9]{1,}\s?(?:AG|GmbH|SA|Ltd|LLC|Inc|KG)\b"
)


# --------------------------------------------------------------------------
# Redaction result type
# --------------------------------------------------------------------------

@dataclass
class RedactionResult:
    """What the redactor returns. The gateway sends `redacted_text` to the
    upstream model and uses `rehydration` to put real values back into the
    response."""
    original_text: str
    redacted_text: str
    rehydration: dict[str, str] = field(default_factory=dict)  # token → original
    stats: dict[str, int] = field(default_factory=dict)         # entity_type → count
    backend: str = "regex"     # "regex" | "presidio"

    def rehydrate(self, response_text: str) -> str:
        """Reverse the redaction in a response. Idempotent — a response
        with no tokens passes through unchanged."""
        out = response_text
        # Process longest tokens first so [VENDOR_0010] is replaced before [VENDOR_001]
        for token in sorted(self.rehydration.keys(), key=len, reverse=True):
            out = out.replace(token, self.rehydration[token])
        return out


# --------------------------------------------------------------------------
# Regex backend — always available, no install needed
# --------------------------------------------------------------------------

class _RegexBackend:
    """Pattern-only redactor. Misses free-form names, but it's good enough
    for first-run demos and as a fallback when Presidio's model isn't
    available."""

    NAME = "regex"

    def redact(self, text: str) -> RedactionResult:
        result = RedactionResult(original_text=text, redacted_text=text, backend=self.NAME)
        counter: dict[str, int] = {}

        # Apply finance patterns first — they're more specific
        for entity_type, pattern in _FINANCE_PATTERNS.items():
            result.redacted_text = self._replace_pattern(
                result.redacted_text, pattern, entity_type, result.rehydration, counter
            )

        # Then "vendor-like" capitalised AG/GmbH/etc.
        result.redacted_text = self._replace_pattern(
            result.redacted_text, _VENDOR_LIKE_RE, "VENDOR", result.rehydration, counter
        )

        result.stats = counter
        return result

    @staticmethod
    def _replace_pattern(
        text: str,
        pattern: re.Pattern,
        entity_type: str,
        rehydration: dict[str, str],
        counter: dict[str, int],
    ) -> str:
        def _repl(match: re.Match) -> str:
            original = match.group(0)
            # Reuse the same token if we've seen this exact value before
            # (stable mapping per request → idempotent rehydration)
            existing = next((tok for tok, val in rehydration.items() if val == original), None)
            if existing:
                return existing
            counter[entity_type.lower()] = counter.get(entity_type.lower(), 0) + 1
            token = f"[{entity_type}_{counter[entity_type.lower()]:04d}]"
            rehydration[token] = original
            return token
        return pattern.sub(_repl, text)


# --------------------------------------------------------------------------
# Presidio backend — production-quality
#
# Lazily imported so the gateway boots without it. If installation fails or
# spaCy model is missing, we fall back to the regex backend with a warning.
# --------------------------------------------------------------------------

class _PresidioBackend:
    NAME = "presidio"

    def __init__(self) -> None:
        # Import here so the module is loadable even if Presidio isn't installed
        from presidio_analyzer import AnalyzerEngine, RecognizerRegistry, PatternRecognizer, Pattern
        from presidio_anonymizer import AnonymizerEngine
        from presidio_anonymizer.entities import OperatorConfig

        registry = RecognizerRegistry()
        registry.load_predefined_recognizers()

        # Add our finance-domain recognisers on top of Presidio's defaults
        for entity_type, pattern in _FINANCE_PATTERNS.items():
            registry.add_recognizer(
                PatternRecognizer(
                    supported_entity=f"CALIBER_{entity_type}",
                    patterns=[Pattern(name=entity_type, regex=pattern.pattern, score=0.85)],
                )
            )

        self._analyzer = AnalyzerEngine(registry=registry)
        self._anonymizer = AnonymizerEngine()
        self._OperatorConfig = OperatorConfig

    def redact(self, text: str) -> RedactionResult:
        result = RedactionResult(original_text=text, redacted_text=text, backend=self.NAME)
        analyses = self._analyzer.analyze(text=text, language="en")
        # Build a stable token map. Presidio gives us spans; we re-walk the
        # text to produce the same {token: original} mapping shape the
        # regex backend produces.
        counter: dict[str, int] = {}
        spans = sorted(analyses, key=lambda a: a.start)
        out_parts: list[str] = []
        cursor = 0
        for span in spans:
            etype = span.entity_type.replace("CALIBER_", "")
            original = text[span.start:span.end]
            existing = next((tok for tok, val in result.rehydration.items() if val == original), None)
            if existing:
                token = existing
            else:
                counter[etype.lower()] = counter.get(etype.lower(), 0) + 1
                token = f"[{etype}_{counter[etype.lower()]:04d}]"
                result.rehydration[token] = original
            out_parts.append(text[cursor:span.start])
            out_parts.append(token)
            cursor = span.end
        out_parts.append(text[cursor:])
        result.redacted_text = "".join(out_parts)
        result.stats = counter
        return result


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

class Redactor:
    """Top-level redactor. Picks the best backend available at construction.

    Usage:
        r = Redactor()                       # auto-picks Presidio if present
        out = r.redact("invoice from BerglandRealtyAG for CHF 21090.30")
        # out.redacted_text  → "invoice from [VENDOR_0001] for [AMOUNT_0001]"
        # out.rehydration    → {"[VENDOR_0001]": "BerglandRealtyAG", ...}
        # out.stats          → {"vendor": 1, "amount": 1}

    To rehydrate a model response:
        clean_response = out.rehydrate(model_response_text)
    """

    def __init__(self, prefer: str | None = None) -> None:
        self._backend = self._pick_backend(prefer)

    @property
    def backend_name(self) -> str:
        return self._backend.NAME

    def redact(self, text: str) -> RedactionResult:
        return self._backend.redact(text or "")

    @staticmethod
    def _pick_backend(prefer: str | None):
        # "prefer" lets tests force the regex backend; production should
        # leave it None.
        if prefer == "regex":
            return _RegexBackend()
        try:
            return _PresidioBackend()
        except Exception:
            # Presidio missing or its spaCy model isn't downloaded — fall
            # back. The gateway logs this on startup so it's visible.
            return _RegexBackend()


# --------------------------------------------------------------------------
# CLI: quick sanity check
# --------------------------------------------------------------------------
# Run: python3 redactor.py "your test string here"

if __name__ == "__main__":
    import json, sys
    text = " ".join(sys.argv[1:]) or (
        "Summarise this invoice from BerglandRealtyAG for CHF 21090.30 charged "
        "to Meridian Alpha AG, account 84.008.505.147.1. Email "
        "treasurer@meridian.com if you have questions. Sygnum ref "
        "zv20260521/192940/1."
    )
    r = Redactor()
    out = r.redact(text)
    print(f"# backend: {out.backend}")
    print(f"# stats:   {out.stats}")
    print(f"# original:\n{out.original_text}")
    print(f"# redacted:\n{out.redacted_text}")
    print(f"# rehydration map:\n{json.dumps(out.rehydration, indent=2)}")
