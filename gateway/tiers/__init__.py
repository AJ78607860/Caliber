"""Tier adapters — each backend that can answer a prompt.

Three adapters, one common interface (see base.py):
  - PublicTier   → Claude/GPT API, no redaction (or demo canned response)
  - PrivateTier  → Claude/GPT API, payload redacted before send + rehydrated after
  - SealedTier   → local model (Ollama/vLLM), never egresses

The gateway picks one per request based on policy (see router.py).
"""
from .base import Tier, TierResponse, TierError
from .public import PublicTier
from .private import PrivateTier
from .sealed import SealedTier

__all__ = ["Tier", "TierResponse", "TierError", "PublicTier", "PrivateTier", "SealedTier"]
