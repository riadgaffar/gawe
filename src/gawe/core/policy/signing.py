"""Signed policy decisions.

The executor accepts only a PolicyDecision whose HMAC verifies under the policy signing key and
whose action_hash matches the action about to run. The LLM never holds this key, so it cannot
fabricate an authorization.
"""

import hashlib
import hmac
import os
from collections.abc import Sequence

from gawe.core.canonical import canonical_json
from gawe.core.models import PolicyDecision, PolicyOutcome

_DOMAIN = b"gawe.decision.v1\n"
MIN_KEY_BYTES = 32
KEY_ENV_VAR = "GAWE_POLICY_SIGNING_KEY"


class PolicySigningError(Exception):
    """Base class for signing failures."""


class ForgedDecisionError(PolicySigningError):
    """Signature does not verify: the decision was altered or not issued by the policy engine."""


class ActionMismatchError(PolicySigningError):
    """A valid decision, but for a different action than the one being executed."""


class PolicySigner:
    def __init__(self, key: bytes) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"signing key must be at least {MIN_KEY_BYTES} bytes")
        self._key = key

    @classmethod
    def from_env(cls, var: str = KEY_ENV_VAR) -> "PolicySigner":
        raw = os.environ.get(var, "").strip()
        if not raw:
            raise PolicySigningError(f"{var} is not set")
        try:
            key = bytes.fromhex(raw)
        except ValueError as exc:
            raise PolicySigningError(f"{var} must be hex-encoded") from exc
        try:
            return cls(key)
        except ValueError as exc:
            raise PolicySigningError(str(exc)) from exc

    def _mac(
        self,
        *,
        step_id: str,
        action_hash: str,
        outcome: PolicyOutcome,
        rule_ids: Sequence[str],
        reason: str,
        approver_role: str | None,
    ) -> str:
        body = canonical_json(
            {
                "step_id": step_id,
                "action_hash": action_hash,
                "outcome": outcome,
                "rule_ids": list(rule_ids),
                "reason": reason,
                "approver_role": approver_role,
            }
        )
        return hmac.new(self._key, _DOMAIN + body, hashlib.sha256).hexdigest()

    def seal(
        self,
        *,
        step_id: str,
        action_hash: str,
        outcome: PolicyOutcome,
        rule_ids: Sequence[str],
        reason: str,
        approver_role: str | None = None,
    ) -> PolicyDecision:
        signature = self._mac(
            step_id=step_id,
            action_hash=action_hash,
            outcome=outcome,
            rule_ids=rule_ids,
            reason=reason,
            approver_role=approver_role,
        )
        return PolicyDecision(
            step_id=step_id,
            action_hash=action_hash,
            outcome=outcome,
            rule_ids=tuple(rule_ids),
            reason=reason,
            approver_role=approver_role,
            signature=signature,
        )

    def verify(self, decision: PolicyDecision) -> bool:
        expected = self._mac(
            step_id=decision.step_id,
            action_hash=decision.action_hash,
            outcome=decision.outcome,
            rule_ids=decision.rule_ids,
            reason=decision.reason,
            approver_role=decision.approver_role,
        )
        return hmac.compare_digest(expected, decision.signature)

    def require_valid(self, decision: PolicyDecision, *, expected_action_hash: str) -> None:
        """Raise unless the decision is authentic and is for exactly this action."""
        if not self.verify(decision):
            raise ForgedDecisionError("policy decision signature is invalid")
        if not hmac.compare_digest(decision.action_hash, expected_action_hash):
            raise ActionMismatchError("policy decision is for a different action")
