"""Sealed tier — local model. Raw customer data never egresses.

WHEN TO USE IT
--------------
For tenant policies that say "this kind of data NEVER goes to a public API
even redacted." Banking transaction-level data, trade secrets, board
materials, M&A drafts — anything where the customer's regulator or risk
committee has said no.

THREE BACKENDS
--------------
Selected via SEALED_BACKEND env var:

  1. "mock"   — canned response for week-1 demos and unit tests.
                No model needed. The audit log clearly marks mock responses.
  2. "ollama" — POST to a local Ollama server (http://localhost:11434/api/chat).
                Recommended for laptop development. Install Ollama, pull a model
                (`ollama pull llama3.2:3b`), and you're done.
  3. "vllm"   — POST to a vLLM OpenAI-compatible endpoint. This is what
                production runs on (H100s in your VPC, single-tenant per
                enterprise customer for the most paranoid).

Adding a fourth backend (TGI, llama.cpp server, MLX) means adding a new
`_call_xxx` method here. The interface stays the same.

WHAT THE TRUST BOUNDARY LOOKS LIKE
----------------------------------
For sealed-tier calls, the path is:

   gateway process  ─►  local network  ─►  vllm/ollama process
                                                    │
                                                    ▼
                                            response back, no internet

If anyone runs `tcpdump` on the gateway's egress while a sealed call is in
flight, they should see zero packets leaving the trust boundary. Verify
this on every deploy with the smoke test in tests/test_sealed_egress.py.
"""

from __future__ import annotations

import os
import httpx
from .base import Tier, TierResponse, TierError


class SealedTier:
    name = "sealed"

    def __init__(
        self,
        *,
        backend: str | None = None,
        url: str | None = None,
        model: str | None = None,
    ) -> None:
        self._backend = (backend or os.environ.get("SEALED_BACKEND", "mock")).lower()
        self._url = (url or os.environ.get("SEALED_BACKEND_URL", "http://localhost:11434")).rstrip("/")
        self._model = model or os.environ.get("SEALED_MODEL", "llama3.2:3b")

    def call(self, *, messages, model, max_tokens=1024, system=None) -> TierResponse:
        # `model` here is the model the caller requested (Anthropic-shaped).
        # The sealed tier uses its own local model — we record the mismatch
        # in `model_used` so the audit trail is honest.
        if self._backend == "mock":
            return self._mock_response(messages, model)
        if self._backend == "ollama":
            return self._call_ollama(messages, max_tokens, system)
        if self._backend == "vllm":
            return self._call_vllm(messages, max_tokens, system)
        raise TierError(f"unknown SEALED_BACKEND={self._backend!r}")

    # ----------------------------------------------------------------------
    # Backend implementations
    # ----------------------------------------------------------------------

    def _call_ollama(self, messages, max_tokens, system) -> TierResponse:
        """Ollama's /api/chat endpoint. Native chat shape, easy."""
        payload = {
            "model": self._model,
            "messages": (
                [{"role": "system", "content": system}] if system else []
            ) + [{"role": m["role"], "content": m.get("content", "")} for m in messages],
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        try:
            resp = httpx.post(f"{self._url}/api/chat", json=payload, timeout=120)
            resp.raise_for_status()
            body = resp.json()
            text = body.get("message", {}).get("content", "")
            return TierResponse(
                text=text,
                model_used=self._model,
                tier_name=self.name,
                backend="ollama",
                usage={
                    "input_tokens": body.get("prompt_eval_count", 0),
                    "output_tokens": body.get("eval_count", 0),
                },
            )
        except httpx.HTTPError as e:
            raise TierError(
                f"sealed-tier (ollama) call failed: {e}. "
                f"Is Ollama running at {self._url}? Start with `ollama serve`."
            ) from e

    def _call_vllm(self, messages, max_tokens, system) -> TierResponse:
        """vLLM's OpenAI-compatible /v1/chat/completions endpoint."""
        payload = {
            "model": self._model,
            "messages": (
                [{"role": "system", "content": system}] if system else []
            ) + [{"role": m["role"], "content": m.get("content", "")} for m in messages],
            "max_tokens": max_tokens,
        }
        try:
            resp = httpx.post(f"{self._url}/v1/chat/completions", json=payload, timeout=120)
            resp.raise_for_status()
            body = resp.json()
            text = body["choices"][0]["message"]["content"]
            usage = body.get("usage", {})
            return TierResponse(
                text=text,
                model_used=self._model,
                tier_name=self.name,
                backend="vllm",
                usage={
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                },
            )
        except httpx.HTTPError as e:
            raise TierError(
                f"sealed-tier (vllm) call failed: {e}. "
                f"Is vLLM serving at {self._url}/v1/?"
            ) from e

    def _mock_response(self, messages, model) -> TierResponse:
        """Deterministic mock for demos + tests. Marked clearly in the audit
        log so we can't accidentally claim sealed-tier in production while
        running on a mock."""
        last = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        text = (
            "[SEALED TIER — MOCK MODEL]\n\n"
            "This response was generated locally inside the trust boundary. "
            "No data was sent to any external API.\n\n"
            "In production this tier runs Llama 3.x via Ollama (dev) or vLLM "
            "on H100 GPUs (production), inside a VPC with no egress to the "
            "public internet.\n\n"
            f"Your prompt was {len(last)} characters; first 80: "
            f"\"{last[:80]}{'…' if len(last) > 80 else ''}\""
        )
        return TierResponse(
            text=text,
            model_used="sealed-mock",
            tier_name=self.name,
            backend="mock",
            usage={"input_tokens": 0, "output_tokens": 0},
        )
