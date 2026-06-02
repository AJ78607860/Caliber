"""Config — env vars + per-tenant YAML loader.

ENV VARS
--------
See .env.example for the full list. config.py reads .env if present (via
python-dotenv) but doesn't require it; environment variables already in
the process win over the .env file.

PER-TENANT YAML
---------------
Each tenant gets a file at config/tenants/<id>.yaml. The router reads it
to build a TenantPolicy on every request. Yes, every request — the file
is small and disk reads are cached by the OS. When we move to many
tenants, we'll add an in-memory cache with a file-mtime invalidation; not
needed yet.

Example file structure (config/tenants/demo.yaml):

    name: Demo Tenant
    default_tier: public
    allow_tier_override_via_header: true   # demo mode only — production: false
    rules:
      - if_entity_in: [SWISS_ACCT, SYGNUM_REF, IBAN]
        then_tier: sealed
      - if_max_entities_gt: 0
        then_tier: private
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

from router import TenantPolicy


# Load .env if present. Idempotent; safe to call multiple times.
_dotenv_path = Path(__file__).parent / ".env"
if _dotenv_path.exists():
    load_dotenv(_dotenv_path)


@dataclass
class Settings:
    """Process-wide settings. Built once at startup."""
    anthropic_api_key: str = ""
    sealed_backend: str = "mock"          # mock | ollama | vllm
    sealed_backend_url: str = "http://localhost:11434"
    sealed_model: str = "llama3.2:3b"
    port: int = 8800
    log_level: str = "INFO"
    audit_dir: Path = Path("../audit")
    tenants_dir: Path = Path("../config/tenants")
    default_tenant: str = "demo"

    @classmethod
    def from_env(cls) -> "Settings":
        here = Path(__file__).parent
        # Path defaults are relative to gateway/ — resolve to absolute so
        # the service works regardless of cwd.
        audit_dir = Path(os.environ.get("CALIBER_AUDIT_DIR", "../audit"))
        if not audit_dir.is_absolute():
            audit_dir = (here / audit_dir).resolve()
        tenants_dir = Path(os.environ.get("CALIBER_TENANTS_DIR", "../config/tenants"))
        if not tenants_dir.is_absolute():
            tenants_dir = (here / tenants_dir).resolve()
        return cls(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            sealed_backend=os.environ.get("SEALED_BACKEND", "mock"),
            sealed_backend_url=os.environ.get("SEALED_BACKEND_URL", "http://localhost:11434"),
            sealed_model=os.environ.get("SEALED_MODEL", "llama3.2:3b"),
            port=int(os.environ.get("CALIBER_PORT", "8800")),
            log_level=os.environ.get("CALIBER_LOG_LEVEL", "INFO"),
            audit_dir=audit_dir,
            tenants_dir=tenants_dir,
            default_tenant=os.environ.get("CALIBER_DEFAULT_TENANT", "demo"),
        )


def load_tenant_policy(tenants_dir: Path, tenant_id: str) -> TenantPolicy:
    """Read a tenant's YAML config. Falls back to defaults if the file
    is missing — the caller can decide whether that's allowed.

    Production should treat "tenant config missing" as an error and refuse
    the request, since it means the tenant isn't actually onboarded.
    Demo accepts it so first-run works without any YAML files at all.
    """
    p = tenants_dir / f"{tenant_id}.yaml"
    if not p.exists():
        return TenantPolicy(name=tenant_id)   # all defaults
    with p.open() as f:
        data = yaml.safe_load(f) or {}
    return TenantPolicy(
        name=data.get("name", tenant_id),
        default_tier=data.get("default_tier", "public"),
        rules=data.get("rules"),
        allow_tier_override_via_header=bool(
            data.get("allow_tier_override_via_header", False)
        ),
    )
