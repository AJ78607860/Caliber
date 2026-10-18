"""Versioned AP agent contract and action validation (additive, no live execution)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
import re


class APAction(str, Enum):
    INVOICE_EXTRACT = "invoice.extract"
    INVOICE_VALIDATE = "invoice.validate"
    PO_MATCH = "purchase_order.match"
    GL_CODING_PROPOSE = "gl_coding.propose"
    PAYMENT_PROPOSE = "payment.propose"


@dataclass(frozen=True)
class APAgentContract:
    agent_id: str
    version: str
    tenant_id: str
    allowed_actions: frozenset[APAction]
    supervisor: str = "ap-manager"
    may_approve: bool = False
    may_release_payment: bool = False

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.tenant_id):
            raise ValueError("invalid tenant identifier")
        if self.may_approve or self.may_release_payment:
            raise ValueError("AP worker agents cannot approve or release payments")


@dataclass(frozen=True)
class APProposal:
    tenant_id: str
    invoice_id: str
    action: APAction
    amount: Decimal
    currency: str
    evidence_id: str
    idempotency_key: str


def validate_proposal(contract: APAgentContract, proposal: APProposal) -> None:
    if proposal.tenant_id != contract.tenant_id:
        raise PermissionError("cross-tenant AP proposal")
    if proposal.action not in contract.allowed_actions:
        raise PermissionError("action not authorised")
    if not isinstance(proposal.amount, Decimal) or not proposal.amount.is_finite() or proposal.amount < 0:
        raise ValueError("invalid amount")
    if not re.fullmatch(r"[A-Z]{3}", proposal.currency):
        raise ValueError("invalid currency")
    if not all((proposal.invoice_id, proposal.evidence_id, proposal.idempotency_key)):
        raise ValueError("missing evidence or idempotency reference")
