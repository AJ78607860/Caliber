"""Common interface every tier implements.

Why a base class instead of plain functions: tier adapters need consistent
inputs (prompt, model, max_tokens) and outputs (text, model used, usage
stats, source label) so the router can swap one for another without the
gateway caring. Adding a new tier (e.g. an Azure OpenAI deployment for an
enterprise customer) means subclassing Tier; nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class TierResponse:
    """What every tier returns. The gateway maps this to an Anthropic-shaped
    response on the wire."""
    text: str                                # the model's response (post-rehydration if applicable)
    model_used: str                          # actual model that ran (may differ from requested)
    tier_name: str                           # "public" | "private" | "sealed"
    backend: str                             # "anthropic" | "openai" | "ollama" | "vllm" | "mock"
    usage: dict = field(default_factory=dict)  # {"input_tokens": N, "output_tokens": N} when known
    raw: dict = field(default_factory=dict)    # raw upstream payload, for debugging — not logged


class TierError(Exception):
    """Raised when a tier can't fulfil a request. Caller decides whether to
    retry, fall back to another tier, or surface to the user."""
    pass


class Tier(Protocol):
    """Every tier exposes the same call shape. Keep this small; richer
    features (streaming, tool use, etc.) live on the concrete adapters
    and bubble up only after we have a clear cross-tier story."""

    name: str            # "public" | "private" | "sealed"

    def call(
        self,
        *,
        messages: list[dict],     # Anthropic-shape: [{"role": "user", "content": "..."}]
        model: str,
        max_tokens: int = 1024,
        system: str | None = None,
    ) -> TierResponse: ...
