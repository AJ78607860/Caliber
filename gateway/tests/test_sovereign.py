"""Standalone unit tests for additive Sovereign policy primitives."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sovereign import DataClass, SovereignDenied, SovereignPolicy, VerifiedPrincipal, authorize, model_route


def principal(tenant="alpha", verified=True, permissions=frozenset({"model:invoke"})):
    return VerifiedPrincipal("tester", tenant, permissions, verified)


def test_rejects_unverified():
    with pytest.raises(SovereignDenied):
        authorize(principal(verified=False), "alpha", "model:invoke")


def test_rejects_cross_tenant():
    with pytest.raises(SovereignDenied):
        authorize(principal(), "beta", "model:invoke")


def test_rejects_missing_permission():
    with pytest.raises(SovereignDenied):
        authorize(principal(permissions=frozenset()), "alpha", "model:invoke")


def test_private_model_allowed():
    assert model_route(principal(), SovereignPolicy("alpha"), DataClass.RESTRICTED, "sealed", classification_complete=True, redaction_complete=False) == "sealed"


def test_external_default_denied():
    with pytest.raises(SovereignDenied):
        model_route(principal(), SovereignPolicy("alpha"), DataClass.PUBLIC, "gpt", classification_complete=True, redaction_complete=True)


def test_external_requires_complete_redaction():
    p = SovereignPolicy("alpha", frozenset({"gpt"}), frozenset({DataClass.PUBLIC}), external_egress_enabled=True)
    with pytest.raises(SovereignDenied):
        model_route(principal(), p, DataClass.PUBLIC, "gpt", classification_complete=True, redaction_complete=False)


def test_external_requires_allowlisted_data_class():
    p = SovereignPolicy("alpha", frozenset({"gpt"}), frozenset({DataClass.PUBLIC}), external_egress_enabled=True)
    with pytest.raises(SovereignDenied):
        model_route(principal(), p, DataClass.RESTRICTED, "gpt", classification_complete=True, redaction_complete=True)


def test_external_allowed_when_all_controls_satisfied():
    p = SovereignPolicy("alpha", frozenset({"gpt"}), frozenset({DataClass.PUBLIC}), external_egress_enabled=True)
    assert model_route(principal(), p, DataClass.PUBLIC, "gpt", classification_complete=True, redaction_complete=True) == "gpt"


def test_missing_classification_fails_closed():
    with pytest.raises(SovereignDenied):
        model_route(principal(), SovereignPolicy("alpha"), DataClass.PUBLIC, "sealed", classification_complete=False, redaction_complete=True)
