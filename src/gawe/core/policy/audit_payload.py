"""The `policy.decided` audit payload, built from the stored decision alone.

Events store only a hash of this payload, so it must be a pure function of the decision: anyone
holding a stored decision can rebuild the payload and match it to the audit trail. Format 1
decisions keep the payload shape recorded before M16b, so existing chains still match.
"""

from typing import Any

from gawe.core.models import PolicyDecision


def decided_payload(decision: PolicyDecision) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "step_id": decision.step_id,
        "outcome": decision.outcome,
        "rule_ids": list(decision.rule_ids),
        "action_hash": decision.action_hash,
        "approver_role": decision.approver_role,
    }
    if decision.format == 2:
        payload["format"] = 2
        payload["checks"] = [check.model_dump() for check in decision.checks]
        payload["policy_digest"] = decision.policy_digest
    return payload
