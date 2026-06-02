"""Caliber gateway — FastAPI entrypoint.

WHAT THIS SERVICE DOES
----------------------
Accepts Anthropic-API-shaped requests at /v1/messages, runs them through
classifier → redactor → router → tier adapter → audit log, returns the
model's response in the same Anthropic shape.

WHY ANTHROPIC-SHAPED
--------------------
Most customers using Claude can switch to Caliber by changing two env vars
in their app:
    ANTHROPIC_API_KEY = <their Caliber API key, not their Anthropic one>
    ANTHROPIC_BASE_URL = https://gateway.caliber.example/v1
No SDK changes, no prompt changes, no model changes. The anthropic-python
SDK and similar will work unmodified. This is the single biggest
adoption lever the product has.

ENDPOINTS
---------
POST /v1/messages           — main entrypoint (Anthropic-shaped)
GET  /health                — uptime + degraded-mode flags
GET  /v1/audit/recent       — JSON of the last 100 audit entries (for the dashboard)

AUTH (not yet implemented)
---------------------------
For v1 we accept X-Caliber-Tenant as a header and trust it. This is fine
for the laptop demo and for a single-tenant pilot. Before we onboard a
second customer, this becomes per-tenant API keys with a `caliber_keys`
table behind it. See docs/next-engineer.md → "Auth roadmap."

RUN
---
    python3 -m uvicorn app:app --reload --port 8800
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from audit import AuditLog
from config import Settings, load_tenant_policy
from redactor import Redactor
from router import Router, RoutingDecision
from tiers import PublicTier, PrivateTier, SealedTier, TierError


# ----- Boot -----

settings = Settings.from_env()
logging.basicConfig(level=settings.log_level, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("caliber.gateway")

redactor = Redactor()
log.info("redactor backend: %s", redactor.backend_name)

# One audit log file per tenant in production; one shared file for the
# demo. config.audit_dir is the parent; per-tenant subdir created lazily.
settings.audit_dir.mkdir(parents=True, exist_ok=True)
_audit_logs: dict[str, AuditLog] = {}


def _audit_for(tenant: str) -> AuditLog:
    if tenant not in _audit_logs:
        per_tenant = settings.audit_dir / f"{tenant}.jsonl"
        # Also keep a shared 'audit.jsonl' for the demo dashboard simplicity
        _audit_logs[tenant] = AuditLog(per_tenant)
    return _audit_logs[tenant]


# Shared rolling-window log read by the dashboard
shared_log = AuditLog(settings.audit_dir / "audit.jsonl")

router = Router()
public_tier = PublicTier(api_key=settings.anthropic_api_key)
private_tier = PrivateTier(api_key=settings.anthropic_api_key, redactor=redactor)
sealed_tier = SealedTier(
    backend=settings.sealed_backend,
    url=settings.sealed_backend_url,
    model=settings.sealed_model,
)
TIER_BY_NAME = {
    "public":  public_tier,
    "private": private_tier,
    "sealed":  sealed_tier,
}


# ----- App -----

app = FastAPI(
    title="Caliber LLM Gateway",
    description="Privacy-preserving LLM gateway. Classifies, redacts, routes, audits.",
    version="0.1.0",
)

# CORS — wide open for the laptop demo. Production: restrict to the
# customer's known origins (configured per-tenant).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    """Surfaces degraded modes so you can see the gateway's posture at a
    glance. Sales-call golden rule: never demo from a degraded gateway."""
    return {
        "ok": True,
        "redactor_backend": redactor.backend_name,
        "sealed_backend": settings.sealed_backend,
        "demo_mode": not settings.anthropic_api_key,
        "audit_dir": str(settings.audit_dir),
    }


@app.post("/v1/messages")
async def create_message(
    body: dict,
    request: Request,
    x_caliber_tenant: str | None = Header(default=None),
    x_caliber_user: str | None = Header(default=None),
    x_caliber_tier: str | None = Header(default=None),
):
    """Anthropic-API-shaped entry point.

    Request body (subset of Anthropic's spec we support today):
        {
          "model": "claude-sonnet-4-6",
          "max_tokens": 1024,
          "system": "optional",
          "messages": [{"role": "user", "content": "..."}]
        }
    """
    started = time.time()
    request_id = f"req_{uuid.uuid4().hex[:16]}"
    tenant = x_caliber_tenant or settings.default_tenant
    policy = load_tenant_policy(settings.tenants_dir, tenant)

    # Validate minimum required fields. Don't bother with a full Pydantic
    # model yet — we want to forward unknown fields untouched to Anthropic
    # once we wire that through; strict validation now would lock us in.
    model = body.get("model") or "claude-sonnet-4-6"
    max_tokens = int(body.get("max_tokens", 1024))
    system = body.get("system")
    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail="messages[] is required")

    # 1. CLASSIFY — redact the prompt to see what entities exist.
    #    For the public tier we'll discard the redacted text and send the
    #    original; the redaction is purely for sensitivity scoring there.
    #    For private/sealed we use the stats AND the redacted text.
    last_user_text = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )
    classification = redactor.redact(
        last_user_text if isinstance(last_user_text, str) else ""
    )

    # 2. ROUTE — pick a tier based on stats + tenant policy + optional override.
    decision: RoutingDecision = router.decide(
        redaction_stats=classification.stats,
        tenant_policy=policy,
        explicit_tier=x_caliber_tier,
    )
    chosen = TIER_BY_NAME.get(decision.tier)
    if chosen is None:
        raise HTTPException(status_code=500, detail=f"unknown tier: {decision.tier}")

    # 3. CALL — invoke the chosen tier.
    error_msg = None
    response_text = None
    try:
        tier_resp = chosen.call(
            messages=messages,
            model=model,
            max_tokens=max_tokens,
            system=system,
        )
        response_text = tier_resp.text
    except TierError as e:
        error_msg = str(e)
        log.warning("[%s] tier=%s failed: %s", request_id, decision.tier, e)
    except Exception as e:    # noqa: BLE001
        error_msg = f"unexpected: {e!r}"
        log.exception("[%s] unexpected failure", request_id)

    duration_ms = int((time.time() - started) * 1000)

    # 4. AUDIT — log every call, success or failure, before returning.
    entry = shared_log.append(
        tenant=tenant,
        user=x_caliber_user,
        tier=decision.tier,
        model=model,
        request_id=request_id,
        prompt_text=last_user_text if isinstance(last_user_text, str) else "",
        response_text=response_text,
        redacted_prompt_text=(
            classification.redacted_text if decision.tier in ("private",) else None
        ),
        redaction_stats=classification.stats,
        duration_ms=duration_ms,
        status="ok" if error_msg is None else "error",
        error=error_msg,
    )

    if error_msg is not None:
        raise HTTPException(status_code=502, detail={"error": error_msg, "request_id": request_id})

    # 5. RETURN — Anthropic-shaped response so SDKs work unmodified.
    return JSONResponse(
        content={
            "id":      request_id,
            "type":    "message",
            "role":    "assistant",
            "model":   tier_resp.model_used,
            "content": [{"type": "text", "text": tier_resp.text}],
            "stop_reason": "end_turn",
            "usage":   tier_resp.usage,
            # Caliber-specific metadata (under x_caliber to avoid collisions
            # with future Anthropic fields):
            "x_caliber": {
                "tier":           decision.tier,
                "tier_reason":    decision.reason,
                "backend":        tier_resp.backend,
                "tenant":         tenant,
                "redaction":      classification.stats,
                "duration_ms":    duration_ms,
                "audit_hash":     entry.hash,
            },
        }
    )


@app.get("/v1/audit/recent")
def audit_recent(limit: int = 100):
    """Last N audit entries, newest first. Used by the dashboard.

    Note: returns metadata only — prompt and response hashes, not the raw
    text. The dashboard combines this with the original requests held
    server-side by the customer's own app (where Caliber never sees them)."""
    rows = list(shared_log.iter_entries())
    return {"entries": rows[-limit:][::-1], "total": len(rows)}


# --------------------------------------------------------------------------
# Static dashboard — same origin as the API so prospects only see one URL.
# Resolves to caliber/dashboard/index.html when the gateway is launched
# from caliber/gateway/.
# --------------------------------------------------------------------------

_DASHBOARD_DIR = (Path(__file__).parent.parent / "dashboard").resolve()
_APP_DIR       = (Path(__file__).parent.parent / "app").resolve()

if _DASHBOARD_DIR.exists():
    log.info("serving admin dashboard from %s", _DASHBOARD_DIR)

    @app.get("/")
    def dashboard_root():
        """Root URL → admin / audit dashboard (the trust surface).

        This is what a customer's CISO and your own ops team look at to
        verify the gateway is doing its job. Audit log, KPI strip,
        try-it panel."""
        return FileResponse(_DASHBOARD_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=_DASHBOARD_DIR), name="static")

if _APP_DIR.exists():
    log.info("serving customer app from %s", _APP_DIR)

    @app.get("/app")
    @app.get("/app/")
    def customer_app_root():
        """Customer-facing application — the operator surface.

        This is what end-users (CFO, controller, ops lead) see and use
        every day. Finance overview + AI Assistant pane. Every assistant
        response carries a tier badge + audit-hash link back to the
        admin dashboard's audit log."""
        return FileResponse(_APP_DIR / "index.html")

    app.mount("/app/static", StaticFiles(directory=_APP_DIR), name="app_static")

# --------------------------------------------------------------------------
# Demo data mount — the customer app ships with a self-contained copy of
# the demo tenant's snapshots at Caliber/app/data/. The dashboard's
# relative paths (../atlas_cache/, ../swiss_cache/, etc.) are rewritten to
# /data/… and served from that local folder, so Caliber doesn't depend on
# any external folder.
#
# In production each tenant's data comes from their own data plane, not
# from a static folder.
#
# Override the source directory with CALIBER_DEMO_DATA_DIR.
# --------------------------------------------------------------------------

_DEMO_DATA_DIR = Path(
    os.environ.get(
        "CALIBER_DEMO_DATA_DIR",
        _APP_DIR / "data"
    )
).expanduser().resolve()

if _DEMO_DATA_DIR.exists():
    log.info("mounting demo data at /data from %s", _DEMO_DATA_DIR)
    app.mount("/data", StaticFiles(directory=_DEMO_DATA_DIR), name="demo_data")
else:
    log.warning(
        "demo data dir not found: %s — customer app dashboard pages will "
        "show empty data.", _DEMO_DATA_DIR,
    )
