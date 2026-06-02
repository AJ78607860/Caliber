"""Router — decides which tier handles each request.

WHY THIS IS A SEPARATE MODULE
-----------------------------
The routing decision is the single place where a customer's privacy policy
meets reality. It deserves its own module so it's easy to audit, easy to
unit-test against adversarial prompts, and easy to swap in a more
sophisticated implementation (e.g. ML-based classifier) without touching
the tiers themselves.

CURRENT LOGIC
-------------
The router takes:
  - the redaction stats (how many sensitive entities were detected)
  - the tenant's policy (from config/tenants/<id>.yaml)
  - any explicit override on the request (X-Caliber-Tier: sealed)

…and returns one of "public" | "private" | "sealed".

Default rule of thumb when no tenant policy says otherwise:

  redaction_stats is empty                                 → public
  redaction_stats contains only non-sensitive types        → public
  redaction_stats contains 1-2 entity types, redactable    → private
  redaction_stats contains "sealed-tier" trigger types     → sealed
                                                             (e.g. trade
                                                             secrets, board
                                                             materials)
  explicit X-Caliber-Tier header                          → that tier

POLICY OVERRIDE
---------------
Tenants can configure their own thresholds in YAML. Example:

    # config/tenants/acme.yaml
    name: ACME Inc
    default_tier: private
    rules:
      - if_entity_in: [SWISS_ACCT, IBAN, SYGNUM_REF]
        then_tier: sealed
      - if_max_entities_gt: 5
        then_tier: sealed
    allow_tier_override_via_header: false  # production: false

EXTENSIBILITY
-------------
Add a new rule type (e.g. "if prompt length exceeds N") by extending
`_apply_rules` below. Rules are evaluated in order; first match wins.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TenantPolicy:
    """Materialised view of a tenant's YAML config. Loaded by config.py."""
    name: str = "demo"
    default_tier: str = "public"
    rules: list[dict] = None
    allow_tier_override_via_header: bool = False

    def __post_init__(self):
        if self.rules is None:
            # Sensible defaults for the demo tenant
            self.rules = [
                {"if_entity_in": ["SWISS_ACCT", "SYGNUM_REF"], "then_tier": "sealed"},
                {"if_max_entities_gt": 0, "then_tier": "private"},
            ]


@dataclass
class RoutingDecision:
    """What the router returns. Carries the chosen tier plus the reason
    so we can log it (and explain it to auditors)."""
    tier: str
    reason: str
    matched_rule: dict | None = None


class Router:
    """Stateless. One per process; cheap to construct."""

    def decide(
        self,
        *,
        redaction_stats: dict[str, int],
        tenant_policy: TenantPolicy,
        explicit_tier: str | None = None,
    ) -> RoutingDecision:
        # 1. Explicit override (only honoured if the tenant policy allows it).
        if explicit_tier:
            if tenant_policy.allow_tier_override_via_header:
                return RoutingDecision(
                    tier=explicit_tier,
                    reason="X-Caliber-Tier header (override allowed by tenant policy)",
                )
            # Override requested but not allowed — log + deny by falling
            # through to rules. The audit log entry will show the attempt.
        # 2. Apply tenant rules in order. First match wins.
        for rule in tenant_policy.rules:
            tier = self._apply_rule(rule, redaction_stats)
            if tier:
                return RoutingDecision(
                    tier=tier,
                    reason=f"matched rule: {rule}",
                    matched_rule=rule,
                )
        # 3. No rule matched — default tier from policy.
        return RoutingDecision(
            tier=tenant_policy.default_tier,
            reason=f"no rule matched; default_tier={tenant_policy.default_tier}",
        )

    @staticmethod
    def _apply_rule(rule: dict, stats: dict[str, int]) -> str | None:
        """Evaluate one rule. Returns the tier name if it matches, else None."""
        # "if_entity_in" — any of these entity types present?
        wanted = rule.get("if_entity_in")
        if wanted:
            # Stats use lowercase keys (vendor, iban). Compare case-insensitively.
            wanted_lower = {w.lower() for w in wanted}
            present = any(k.lower() in wanted_lower for k, v in stats.items() if v > 0)
            if present:
                return rule.get("then_tier")
            # If this is the only condition and it didn't match, the rule
            # doesn't fire — fall through.

        # "if_max_entities_gt" — total entity count exceeds threshold?
        threshold = rule.get("if_max_entities_gt")
        if threshold is not None:
            total = sum(stats.values())
            if total > threshold:
                return rule.get("then_tier")

        return None
