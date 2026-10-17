"""Fail-closed Sovereign authorisation primitives.

This module is additive: existing demo endpoints are NOT automatically secured.
Production integration must replace untrusted tenant headers with verified identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hmac
import re


class SovereignDenied(PermissionError):
    pass


class DataClass(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


_TENANT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass(frozen=True)
class VerifiedPrincipal:
    """Construct only after verifying a signed token or mTLS identity."""
    subject: str
    tenant_id: str
    permissions: frozenset[str]
    verified: bool = False


@dataclass(frozen=True)
class SovereignPolicy:
    tenant_id: str
    allowed_external_models: frozenset[str] = frozenset()
    allowed_external_classes: frozenset[DataClass] = frozenset()
    private_model: str = "sealed"
    external_egress_enabled: bool = False


def authorize(principal: VerifiedPrincipal, tenant_id: str, permission: str) -> None:
    if not principal.verified:
        raise SovereignDenied("unverified principal")
    if not _TENANT_RE.fullmatch(tenant_id):
        raise SovereignDenied("invalid tenant")
    if not hmac.compare_digest(principal.tenant_id, tenant_id):
        raise SovereignDenied("cross-tenant access")
    if permission not in principal.permissions:
        raise SovereignDenied("permission denied")


def model_route(
    principal: VerifiedPrincipal,
    policy: SovereignPolicy,
    data_class: DataClass,
    requested_model: str,
    *,
    classification_complete: bool,
    redaction_complete: bool,
) -> str:
    """Return private model or approved external model; never implicitly permit egress."""
    authorize(principal, policy.tenant_id, "model:invoke")
    if not classification_complete:
        raise SovereignDenied("classification incomplete")
    if not isinstance(data_class, DataClass):
        raise SovereignDenied("invalid data classification")
    if not requested_model:
        raise SovereignDenied("missing model")
    if requested_model == policy.private_model:
        return requested_model
    if not redaction_complete:
        raise SovereignDenied("redaction incomplete")
    if not policy.external_egress_enabled:
        raise SovereignDenied("external model egress disabled")
    if requested_model not in policy.allowed_external_models:
        raise SovereignDenied("model not approved")
    if data_class not in policy.allowed_external_classes:
        raise SovereignDenied("data class not approved for external processing")
    return requested_model
