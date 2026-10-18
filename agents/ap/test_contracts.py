from decimal import Decimal
import pytest
from contracts import APAction, APAgentContract, APProposal, validate_proposal


def test_valid_proposal():
    c = APAgentContract("ap-worker", "1", "alpha", frozenset({APAction.PAYMENT_PROPOSE}))
    p = APProposal("alpha", "inv1", APAction.PAYMENT_PROPOSE, Decimal("120.00"), "GBP", "e1", "k1")
    validate_proposal(c, p)


def test_cross_tenant_denied():
    c = APAgentContract("ap-worker", "1", "alpha", frozenset({APAction.PAYMENT_PROPOSE}))
    p = APProposal("beta", "inv1", APAction.PAYMENT_PROPOSE, Decimal("120.00"), "GBP", "e1", "k1")
    with pytest.raises(PermissionError):
        validate_proposal(c, p)


def test_payment_release_not_allowed():
    with pytest.raises(ValueError):
        APAgentContract("ap-worker", "1", "alpha", frozenset(), may_release_payment=True)


def test_nan_denied():
    c = APAgentContract("ap-worker", "1", "alpha", frozenset({APAction.PAYMENT_PROPOSE}))
    p = APProposal("alpha", "inv1", APAction.PAYMENT_PROPOSE, Decimal("NaN"), "GBP", "e1", "k1")
    with pytest.raises(ValueError):
        validate_proposal(c, p)
