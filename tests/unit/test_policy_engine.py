from typing import Any

import pytest
from tests.helpers import TOOLS, make_ctx, signer

from gawe.core.canonical import action_hash
from gawe.core.policy.engine import RuleEngine
from gawe.core.policy.rules import parse_rules

EMAIL = TOOLS["contracts.send_email"]
OK_ARGS = {"to": "bob@acme.com", "body": "hi"}


def engine(yaml_text: str) -> RuleEngine:
    return RuleEngine(parse_rules(yaml_text), signer())


def test_no_rules_means_default_deny() -> None:
    d = engine("rules: []").evaluate(make_ctx(EMAIL, OK_ARGS, roles=["case_manager"]))
    assert d.outcome == "deny" and d.rule_ids == ("default-deny",)


def test_strongest_effect_wins_regardless_of_rule_order() -> None:
    allow = "- {id: a, effect: allow, when: {tool: contracts.send_email}}"
    ask = (
        "- {id: b, effect: needs_approval, approver_role: legal, "
        "when: {tool: contracts.send_email}}"
    )
    deny = "- {id: c, effect: deny, when: {tool: contracts.send_email}}"
    ctx = make_ctx(EMAIL, OK_ARGS, roles=["case_manager"])

    for order in ([allow, ask, deny], [deny, ask, allow], [ask, allow, deny]):
        d = engine("rules:\n" + "\n".join(order)).evaluate(ctx)
        assert d.outcome == "deny"
        assert d.approver_role is None

    for order in ([allow, ask], [ask, allow]):
        d = engine("rules:\n" + "\n".join(order)).evaluate(ctx)
        assert d.outcome == "needs_approval" and d.approver_role == "legal"


def test_conflicting_approver_roles_fail_closed() -> None:
    d = engine(
        """
rules:
  - {id: a, effect: needs_approval, approver_role: legal, when: {tool: contracts.send_email}}
  - {id: b, effect: needs_approval, approver_role: finance, when: {tool: contracts.send_email}}
"""
    ).evaluate(make_ctx(EMAIL, OK_ARGS, roles=["case_manager"]))
    assert d.outcome == "deny"
    assert d.rule_ids[0] == "engine.conflicting_approvers"
    assert d.approver_role is None


def test_same_approver_role_from_two_rules_is_fine() -> None:
    d = engine(
        """
rules:
  - {id: a, effect: needs_approval, approver_role: legal, when: {tool: contracts.send_email}}
  - {id: b, effect: needs_approval, approver_role: legal, when: {risk_tier: high}}
"""
    ).evaluate(make_ctx(EMAIL, OK_ARGS, roles=["case_manager"]))
    assert d.outcome == "needs_approval" and d.approver_role == "legal"


def test_rbac_applies_before_rules_and_any_one_role_suffices() -> None:
    allow_all = engine("rules:\n  - {id: a, effect: allow, when: {tool: '*'}}")
    denied = allow_all.evaluate(make_ctx(EMAIL, OK_ARGS, roles=["intern"]))
    assert denied.outcome == "deny" and denied.rule_ids == ("rbac.missing_role",)
    assert "intern" not in denied.reason  # decisions do not echo role data
    assert allow_all.evaluate(make_ctx(EMAIL, OK_ARGS, roles=["legal_reviewer"])).outcome == "allow"
    assert allow_all.evaluate(make_ctx(EMAIL, OK_ARGS, roles=["case_manager"])).outcome == "allow"
    assert allow_all.evaluate(make_ctx(EMAIL, OK_ARGS, roles=[])).outcome == "deny"


def test_invalid_args_deny_even_when_a_rule_would_allow() -> None:
    allow_all = engine("rules:\n  - {id: a, effect: allow, when: {tool: '*'}}")
    d = allow_all.evaluate(make_ctx(EMAIL, {"to": 5, "body": "x"}, roles=["case_manager"]))
    assert d.outcome == "deny" and d.rule_ids == ("engine.invalid_args",)
    d = allow_all.evaluate(make_ctx(EMAIL, {**OK_ARGS, "bcc": "x@evil.io"}, roles=["case_manager"]))
    assert d.rule_ids == ("engine.invalid_args",)


def test_evaluation_error_denies() -> None:
    d = engine(
        "rules:\n  - {id: a, effect: allow, when: {args: {to: {domain_in: $nope.list}}}}"
    ).evaluate(make_ctx(EMAIL, OK_ARGS, roles=["case_manager"]))
    assert d.outcome == "deny" and d.rule_ids == ("engine.error",)


def test_decision_binds_to_the_exact_action() -> None:
    ctx = make_ctx(EMAIL, OK_ARGS, roles=["case_manager"], run_id="runA", step_id="stepA")
    d = engine("rules:\n  - {id: a, effect: allow, when: {tool: '*'}}").evaluate(ctx)
    assert d.step_id == "stepA"
    assert d.action_hash == action_hash(
        tool=EMAIL.name,
        version=EMAIL.version,
        resolved_args=OK_ARGS,
        run_id="runA",
        step_id="stepA",
    )


def test_decisions_are_deterministic() -> None:
    eng = engine("rules:\n  - {id: a, effect: allow, when: {tool: '*'}}")
    ctx = make_ctx(EMAIL, OK_ARGS, roles=["case_manager"])
    assert eng.evaluate(ctx) == eng.evaluate(ctx)


def test_different_args_produce_different_hashes() -> None:
    eng = engine("rules:\n  - {id: a, effect: allow, when: {tool: '*'}}")
    a = eng.evaluate(make_ctx(EMAIL, OK_ARGS, roles=["case_manager"]))
    b = eng.evaluate(make_ctx(EMAIL, {**OK_ARGS, "body": "changed"}, roles=["case_manager"]))
    assert a.action_hash != b.action_hash and a.signature != b.signature


def test_deciding_rule_reason_is_used() -> None:
    d = engine(
        """
rules:
  - {id: a, effect: allow, reason: fine, when: {tool: contracts.send_email}}
  - {id: b, effect: deny, reason: blocked on purpose, when: {tool: contracts.send_email}}
"""
    ).evaluate(make_ctx(EMAIL, OK_ARGS, roles=["case_manager"]))
    assert d.reason == "blocked on purpose"


@pytest.mark.parametrize("op", ["in", "domain_in"])
@pytest.mark.parametrize(
    "variables,expected_rule",
    [
        ({"values": []}, "default-deny"),
        ({}, "engine.error"),
        ({"values": "not-a-list"}, "engine.error"),
    ],
)
def test_positive_allow_membership_fails_closed(
    op: str, variables: dict[str, Any], expected_rule: str
) -> None:
    eng = engine(
        "rules: [{id: positive, effect: allow, when: {args: {to: {" + op + ": $values}}}}]"
    )
    decision = eng.evaluate(make_ctx(EMAIL, OK_ARGS, roles=["case_manager"], variables=variables))
    assert decision.outcome == "deny"
    assert decision.rule_ids == (expected_rule,)
    assert signer().verify(decision)
