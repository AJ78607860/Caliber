"""Private tier — Claude API, but every prompt is redacted before send
and rehydrated on the way back.

WHEN TO USE IT
--------------
Prompts where customer data is present BUT the customer's policy says
"redaction is sufficient." Useful when the LLM only needs structural
understanding (e.g. "summarise this invoice's payment terms") not the
literal vendor identity.

THE LOOP
--------
   raw prompt
       │
       ▼
   redactor.redact()  →  redacted_text + rehydration map
       │
       ▼
   Anthropic API call with redacted_text
       │
       ▼
   response_text (may contain redaction tokens like [VENDOR_0001])
       │
       ▼
   rehydration.rehydrate(response_text)
       │
       ▼
   customer sees their real entities

The Anthropic API only ever sees `[VENDOR_0001]`. The mapping back to
"BerglandRealtyAG" never leaves the gateway process. If a future change ever
attaches the rehydration map to the audit log, that's a regression — the
audit log is meant to prove redaction happened, not undo it.

DEMO MODE
---------
Same fallback as PublicTier — no API key → canned response that shows the
redaction visibly. Useful precisely because the demo can prove the
mechanism without sending anything anywhere.
"""

from __future__ import annotations

import os
from .base import Tier, TierResponse, TierError
from redactor import Redactor


class PrivateTier:
    name = "private"

    def __init__(self, *, api_key: str | None = None, redactor: Redactor | None = None) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._redactor = redactor or Redactor()
        self._client = None

    def call(self, *, messages, model, max_tokens=1024, system=None) -> TierResponse:
        # Redact every message's content before sending.
        # We process user messages; system/assistant messages are typically
        # canned policy or model output and don't carry customer data — but
        # we still redact them defensively in case the caller embedded data.
        redacted_messages: list[dict] = []
        combined_rehydration: dict[str, str] = {}
        stats: dict[str, int] = {}
        for m in messages:
            content = m.get("content", "")
            if isinstance(content, str):
                result = self._redactor.redact(content)
                redacted_messages.append({**m, "content": result.redacted_text})
                # Merge per-message rehydration into the request-level map.
                # Token collisions are impossible because the redactor
                # assigns monotonically increasing counters per call —
                # but to be safe across messages, we re-number.
                for tok, val in result.rehydration.items():
                    if tok not in combined_rehydration:
                        combined_rehydration[tok] = val
                for k, v in result.stats.items():
                    stats[k] = stats.get(k, 0) + v
            else:
                # Non-string content (images, tool results, etc.) — pass through.
                # When we add image PII detection this is where it would land.
                redacted_messages.append(m)

        if not self._api_key:
            return self._demo_response(messages, redacted_messages, combined_rehydration, stats, model)

        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self._api_key)
            except ImportError as e:
                raise TierError(
                    "anthropic package not installed — see PublicTier docstring"
                ) from e

        try:
            kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": redacted_messages,
            }
            if system:
                kwargs["system"] = system
            resp = self._client.messages.create(**kwargs)
            text = "".join(
                block.text for block in resp.content if getattr(block, "type", None) == "text"
            )
            # REHYDRATE — the customer-visible response gets real entities back.
            rehydrated = self._rehydrate_text(text, combined_rehydration)
            return TierResponse(
                text=rehydrated,
                model_used=resp.model,
                tier_name=self.name,
                backend="anthropic",
                usage={
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                    "entities_redacted": sum(stats.values()),
                    **{f"redacted_{k}": v for k, v in stats.items()},
                },
            )
        except Exception as e:
            raise TierError(f"private-tier call failed: {e}") from e

    @staticmethod
    def _rehydrate_text(text: str, rehydration: dict[str, str]) -> str:
        """Inverse of redaction. Longest tokens first to handle [VENDOR_0010]
        before [VENDOR_001]."""
        out = text
        for tok in sorted(rehydration.keys(), key=len, reverse=True):
            out = out.replace(tok, rehydration[tok])
        return out

    @staticmethod
    def _demo_response(
        messages, redacted_messages, rehydration, stats, model,
    ) -> TierResponse:
        last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        last_redacted = next((m["content"] for m in reversed(redacted_messages) if m.get("role") == "user"), "")
        n_entities = sum(stats.values())
        text = (
            "[DEMO MODE — set ANTHROPIC_API_KEY for real responses]\n\n"
            "Private tier received your prompt and redacted "
            f"{n_entities} sensitive entit{'y' if n_entities == 1 else 'ies'} "
            "before it would have been sent to Anthropic.\n\n"
            f"What Anthropic would have seen:\n  {last_redacted}\n\n"
            "What you (the customer) would see in production: the model's "
            "response with your real entity names rehydrated. In demo mode, "
            "no upstream call is made."
        )
        return TierResponse(
            text=text,
            model_used=f"{model} (demo)",
            tier_name="private",
            backend="mock",
            usage={"entities_redacted": n_entities, **{f"redacted_{k}": v for k, v in stats.items()}},
        )
