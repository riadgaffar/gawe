"""Signed policy decisions.

The executor accepts only a PolicyDecision whose HMAC verifies under the policy signing key and
whose action_hash matches the action about to run. The LLM never holds this key, so it cannot
fabricate an authorization.

New decisions are format 2, signed under `gawe.decision.v2`, and also cover the evaluated checks
and the rule-set digest. Format 1 decisions from before M16b verify with their original body.
"""

import hashlib
import hmac
import os
from collections.abc import Sequence
from typing import Any

from gawe.core.canonical import canonical_json
from gawe.core.models import DecisionCheck, PolicyDecision, PolicyOutcome

_DOMAINS = {1: b"gawe.decision.v1\n", 2: b"gawe.decision.v2\n"}
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
        format: int,
        step_id: str,
        action_hash: str,
        outcome: PolicyOutcome,
        rule_ids: Sequence[str],
        reason: str,
        approver_role: str | None,
        checks: Sequence[DecisionCheck] = (),
        policy_digest: str | None = None,
    ) -> str:
        fields: dict[str, Any] = {
            "step_id": step_id,
            "action_hash": action_hash,
            "outcome": outcome,
            "rule_ids": list(rule_ids),
            "reason": reason,
            "approver_role": approver_role,
        }
        if format == 2:
            # The v1 body is unchanged so decisions issued before M16b keep verifying.
            fields["format"] = 2
            fields["checks"] = [check.model_dump() for check in checks]
            fields["policy_digest"] = policy_digest
        body = canonical_json(fields)
        return hmac.new(self._key, _DOMAINS[format] + body, hashlib.sha256).hexdigest()

    def seal(
        self,
        *,
        step_id: str,
        action_hash: str,
        outcome: PolicyOutcome,
        rule_ids: Sequence[str],
        reason: str,
        policy_digest: str,
        approver_role: str | None = None,
        checks: Sequence[DecisionCheck] = (),
    ) -> PolicyDecision:
        """Issue a format 2 decision; earlier formats are only ever verified, never issued."""
        decision = PolicyDecision(
            step_id=step_id,
            action_hash=action_hash,
            outcome=outcome,
            rule_ids=tuple(rule_ids),
            reason=reason,
            approver_role=approver_role,
            signature="",
            format=2,
            checks=tuple(checks),
            policy_digest=policy_digest,
        )
        return decision.model_copy(update={"signature": self._sign(decision)})

    def _sign(self, decision: PolicyDecision) -> str:
        return self._mac(
            format=decision.format,
            step_id=decision.step_id,
            action_hash=decision.action_hash,
            outcome=decision.outcome,
            rule_ids=decision.rule_ids,
            reason=decision.reason,
            approver_role=decision.approver_role,
            checks=decision.checks,
            policy_digest=decision.policy_digest,
        )

    def verify(self, decision: PolicyDecision) -> bool:
        """Verify under the decision's own format only; a failed v2 check never falls back."""
        if decision.format not in _DOMAINS:
            return False
        if decision.format == 1 and (decision.checks or decision.policy_digest is not None):
            return False  # new fields cannot ride on a v1 signature
        if decision.format == 2 and decision.policy_digest is None:
            return False
        return hmac.compare_digest(self._sign(decision), decision.signature)

    def require_valid(self, decision: PolicyDecision, *, expected_action_hash: str) -> None:
        """Raise unless the decision is authentic and is for exactly this action."""
        if not self.verify(decision):
            raise ForgedDecisionError("policy decision signature is invalid")
        if not hmac.compare_digest(decision.action_hash, expected_action_hash):
            raise ActionMismatchError("policy decision is for a different action")
