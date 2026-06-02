"""Public tier — Claude API for prompts containing no customer data.

WHEN TO USE IT
--------------
Anything where the prompt could safely be shown to a stranger: code
generation, "explain X concept," schema inference on dummy data, general Q&A.
The router picks this when the redactor finds zero sensitive entities AND
the tenant's policy permits it.

CONTRACTUAL POSTURE
-------------------
Anthropic API with a Zero-Data-Retention (ZDR) addendum on the contract.
No human review, no training, 30-day retention max (and typically 0 days
for ZDR customers). The customer sees this lineage in their DPA.

DEMO MODE
---------
If ANTHROPIC_API_KEY is not set, this returns a canned response so first-run
demos don't need an API key. The audit log clearly marks demo-mode responses
so you can't accidentally show fake outputs to a real prospect.
"""

from __future__ import annotations

import os
from .base import Tier, TierResponse, TierError


class PublicTier:
    name = "public"

    def __init__(self, *, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        # Defer client creation until first call — lets the gateway boot
        # in demo mode without anthropic-python installed.
        self._client = None

    def call(self, *, messages, model, max_tokens=1024, system=None) -> TierResponse:
        if not self._api_key:
            return self._demo_response(messages, model)

        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self._api_key)
            except ImportError as e:
                raise TierError(
                    "anthropic package not installed — `pip install anthropic` "
                    "or leave ANTHROPIC_API_KEY blank to run in demo mode"
                ) from e

        try:
            kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if system:
                kwargs["system"] = system
            resp = self._client.messages.create(**kwargs)
            # The response.content is a list of blocks; for our gateway shape
            # we concatenate text blocks. Tool use / image blocks come later.
            text = "".join(
                block.text for block in resp.content if getattr(block, "type", None) == "text"
            )
            return TierResponse(
                text=text,
                model_used=resp.model,
                tier_name=self.name,
                backend="anthropic",
                usage={
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                },
                raw={},   # don't keep the raw object — it's already audited
            )
        except Exception as e:
            raise TierError(f"public-tier call failed: {e}") from e

    @staticmethod
    def _demo_response(messages: list[dict], model: str) -> TierResponse:
        """Canned response for first-run demos. Real prospects should see
        real responses — set ANTHROPIC_API_KEY before any sales demo.
        """
        last = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        sample = str(last)[:80] + ("…" if len(str(last)) > 80 else "")
        text = (
            "[DEMO MODE — set ANTHROPIC_API_KEY for real responses]\n\n"
            f"Public tier received your prompt (\"{sample}\"). In a real "
            "deployment this would be answered by Claude via Anthropic's "
            "Zero-Data-Retention API. No customer data is being sent because "
            "the redactor classified this prompt as not sensitive."
        )
        return TierResponse(
            text=text,
            model_used=f"{model} (demo)",
            tier_name="public",
            backend="mock",
            usage={"input_tokens": 0, "output_tokens": 0},
        )
