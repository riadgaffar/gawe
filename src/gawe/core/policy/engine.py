"""The policy engine: evaluates one concrete action and returns a signed decision.

Order of evaluation, all deterministic:
1. RBAC from the tool manifest (acting user must hold at least one required role).
2. The resolved args must satisfy the tool's input schema (defense in depth).
3. Declarative rules. Strongest matching effect wins: deny > needs_approval > allow.
4. Nothing matched: default-deny.

Any evaluation failure yields a deny, never an allow.

A decision names one approver role. If matching needs_approval rules name different roles, the
engine denies (engine.conflicting_approvers) instead of guessing; that is a policy bug to fix.
"""

from collections.abc import Sequence
from typing import Protocol

from jsonschema import Draft202012Validator

from gawe.core.canonical import action_hash
from gawe.core.models import PolicyDecision, PolicyOutcome
from gawe.core.policy.rules import (
    EvaluationContext,
    PolicyEvaluationError,
    Rule,
    rule_matches,
)
from gawe.core.policy.signing import PolicySigner

_RANK: dict[PolicyOutcome, int] = {"allow": 0, "needs_approval": 1, "deny": 2}


class PolicyEngine(Protocol):
    def evaluate(self, ctx: EvaluationContext) -> PolicyDecision: ...


class RuleEngine:
    def __init__(self, rules: Sequence[Rule], signer: PolicySigner) -> None:
        self._rules = tuple(rules)
        self._signer = signer

    def evaluate(self, ctx: EvaluationContext) -> PolicyDecision:
        manifest = ctx.manifest
        digest = action_hash(
            tool=manifest.name,
            version=manifest.version,
            resolved_args=ctx.resolved_args,
            run_id=ctx.run_id,
            step_id=ctx.step_id,
        )

        def seal(
            outcome: PolicyOutcome,
            rule_ids: Sequence[str],
            reason: str,
            approver_role: str | None = None,
        ) -> PolicyDecision:
            return self._signer.seal(
                step_id=ctx.step_id,
                action_hash=digest,
                outcome=outcome,
                rule_ids=rule_ids,
                reason=reason,
                approver_role=approver_role,
            )

        required = set(manifest.required_roles)
        if required and not (ctx.user_roles & required):
            return seal(
                "deny", ["rbac.missing_role"], f"acting user lacks a role for {manifest.name}"
            )

        if any(Draft202012Validator(manifest.input_schema).iter_errors(dict(ctx.resolved_args))):
            return seal("deny", ["engine.invalid_args"], "resolved args do not satisfy tool schema")

        try:
            matched = [rule for rule in self._rules if rule_matches(rule, ctx)]
        except PolicyEvaluationError as exc:
            return seal("deny", ["engine.error"], f"policy evaluation failed: {exc}")

        if not matched:
            return seal("deny", ["default-deny"], "no rule allows this action")

        outcome = max((rule.effect for rule in matched), key=_RANK.__getitem__)
        deciding = [rule for rule in matched if rule.effect == outcome]
        rule_ids = [rule.id for rule in matched]
        reason = next((r.reason for r in deciding if r.reason), f"{outcome} by {deciding[0].id}")
        approver: str | None = None
        if outcome == "needs_approval":
            roles = {rule.approver_role for rule in deciding}
            if len(roles) > 1:
                # One decision can name one approver role. Disagreement is a policy-authoring
                # problem; fail closed rather than pick a winner.
                return seal(
                    "deny",
                    ["engine.conflicting_approvers", *rule_ids],
                    "matching rules require different approver roles",
                )
            approver = next(iter(roles))
        return seal(outcome, rule_ids, reason, approver)
