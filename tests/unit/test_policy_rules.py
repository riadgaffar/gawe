from typing import Any

import pytest
from tests.helpers import TOOLS, make_ctx

from gawe.core.policy.rules import (
    MISSING,
    PolicyEvaluationError,
    Rule,
    RuleConfigError,
    apply_operator,
    email_domain,
    lookup,
    parse_rules,
    rule_matches,
)

GENERIC = TOOLS["contracts.extract_risks"]


def matches(
    op: str, operand: Any, args: dict[str, Any], variables: dict[str, Any] | None = None
) -> bool:
    rule = Rule.model_validate(
        {"id": "t", "effect": "allow", "when": {"args": {"x": {op: operand}}}}
    )
    return rule_matches(rule, make_ctx(GENERIC, args, variables=variables))


# --- operators ---


@pytest.mark.parametrize(
    "op,operand,args,expected",
    [
        ("eq", 5, {"x": 5}, True),
        ("eq", 5, {"x": 6}, False),
        ("eq", "a", {"x": "a"}, True),
        ("eq", 1, {"x": True}, False),
        ("eq", True, {"x": 1}, False),
        ("eq", 5, {}, False),
        ("ne", 5, {"x": 6}, True),
        ("ne", 5, {"x": 5}, False),
        ("ne", 5, {}, True),
        ("in", [1, 2], {"x": 2}, True),
        ("in", [1, 2], {"x": 3}, False),
        ("in", [1, 2], {}, False),
        ("in", [1], {"x": True}, False),
        ("not_in", [1, 2], {"x": 3}, True),
        ("not_in", [1, 2], {"x": 1}, False),
        ("not_in", [1, 2], {}, True),
        ("gt", 10, {"x": 11}, True),
        ("gt", 10, {"x": 10}, False),
        ("gte", 10, {"x": 10}, True),
        ("lt", 10, {"x": 9.5}, True),
        ("lt", 10, {"x": 10}, False),
        ("lte", 10, {"x": 10}, True),
        ("gt", 10, {"x": "99"}, False),
        ("gt", 10, {"x": True}, False),
        ("gt", 10, {"x": None}, False),
        ("gt", 10, {}, False),
        ("exists", True, {"x": None}, True),
        ("exists", True, {}, False),
        ("exists", False, {}, True),
        ("exists", False, {"x": 0}, False),
        ("domain_in", ["acme.com"], {"x": "a@acme.com"}, True),
        ("domain_in", ["acme.com"], {"x": "a@ACME.com"}, True),
        ("domain_in", ["ACME.com"], {"x": "a@acme.com"}, True),
        ("domain_in", ["acme.com"], {"x": "a@sub.acme.com"}, False),
        ("domain_in", ["acme.com"], {}, False),
        ("domain_not_in", ["acme.com"], {"x": "a@other.com"}, True),
        ("domain_not_in", ["acme.com"], {"x": "a@acme.com"}, False),
        ("domain_not_in", ["acme.com"], {}, True),
        ("domain_not_in", ["acme.com"], {"x": 42}, True),
    ],
)
def test_operator(op: str, operand: Any, args: dict[str, Any], expected: bool) -> None:
    assert matches(op, operand, args) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("a@acme.com", "acme.com"),
        ("A.B+tag@Sub.Acme.COM", "sub.acme.com"),
        ("a@acme.com, b@evil.io", None),
        ("b@evil.io;a@acme.com", None),
        ("Bob <a@acme.com>", None),
        ("a@acme.com ", None),
        (" a@acme.com", None),
        ("a@@acme.com", None),
        ("@acme.com", None),
        ("a@", None),
        ("a@-acme.com", None),
        ("", None),
        (None, None),
        (["a@acme.com"], None),
        (42, None),
    ],
)
def test_email_domain(value: Any, expected: str | None) -> None:
    assert email_domain(value) == expected


def test_lookup_nested_and_missing() -> None:
    data = {"a": {"b": {"c": 1}}, "s": "str"}
    assert lookup(data, "a.b.c") == 1
    assert lookup(data, "a.x") is MISSING
    assert lookup(data, "s.len") is MISSING
    assert lookup(data, "zzz") is MISSING


def test_nested_arg_path_matching() -> None:
    rule = Rule.model_validate(
        {"id": "t", "effect": "allow", "when": {"args": {"deal.value": {"gt": 5}}}}
    )
    assert rule_matches(rule, make_ctx(GENERIC, {"deal": {"value": 9}}))
    assert not rule_matches(rule, make_ctx(GENERIC, {"deal": {"value": 1}}))


# --- variables ---


def test_variable_operand_resolves() -> None:
    v = {"tenant": {"internal_domains": ["acme.com"]}}
    assert matches("domain_in", "$tenant.internal_domains", {"x": "a@acme.com"}, v)
    assert not matches("domain_in", "$tenant.internal_domains", {"x": "a@evil.io"}, v)


def test_unresolved_variable_raises() -> None:
    with pytest.raises(PolicyEvaluationError, match="unresolved"):
        matches("domain_in", "$tenant.internal_domains", {"x": "a@acme.com"}, {})


def test_variable_with_wrong_type_raises() -> None:
    with pytest.raises(PolicyEvaluationError):
        matches("in", "$v", {"x": 1}, {"v": "not-a-list"})


# --- tool / class / tier predicates ---


def test_tool_glob_and_exact() -> None:
    def rule(tool: str) -> Rule:
        return Rule.model_validate({"id": "t", "effect": "allow", "when": {"tool": tool}})

    ctx = make_ctx(TOOLS["contracts.send_email"], {"to": "a@acme.com", "body": "x"})
    assert rule_matches(rule("contracts.*"), ctx)
    assert rule_matches(rule("*"), ctx)
    assert rule_matches(rule("contracts.send_email"), ctx)
    assert not rule_matches(rule("crm.*"), ctx)
    assert not rule_matches(rule("contracts"), ctx)


def test_class_and_tier_predicates_are_anded() -> None:
    ctx = make_ctx(TOOLS["contracts.send_email"], {"to": "a@acme.com", "body": "x"})
    both = Rule.model_validate(
        {
            "id": "t",
            "effect": "allow",
            "when": {"side_effect_class": "external", "risk_tier": "high"},
        }
    )
    wrong_tier = Rule.model_validate(
        {
            "id": "t",
            "effect": "allow",
            "when": {"side_effect_class": "external", "risk_tier": "low"},
        }
    )
    assert rule_matches(both, ctx)
    assert not rule_matches(wrong_tier, ctx)


# --- loading and validation ---

VALID = """
rules:
  - id: a
    effect: allow
    when: {tool: x.y}
"""


def test_valid_policy_loads() -> None:
    (rule,) = parse_rules(VALID)
    assert rule.id == "a" and rule.effect == "allow"


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("rules: [", "not valid YAML"),
        ("[]", "mapping"),
        ("rules: []\nextra: 1", "mapping"),
        ("rules: nope", "must be a list"),
        ("rules: [1]", "rule #1"),
        ("rules:\n  - {id: a, effect: allow, when: {}}", "at least one predicate"),
        ("rules:\n  - {id: a, effect: maybe, when: {tool: x}}", "rule #1"),
        ("rules:\n  - {id: a, effect: allow, when: {tool: x}, surprise: 1}", "rule #1"),
        ("rules:\n  - {id: A, effect: allow, when: {tool: x}}", "rule #1"),
        ("rules:\n  - {id: a, effect: needs_approval, when: {tool: x}}", "approver_role"),
        ("rules:\n  - {id: a, effect: allow, approver_role: r, when: {tool: x}}", "only valid"),
        ("rules:\n  - {id: rbac.x, effect: allow, when: {tool: x}}", "reserved"),
        ("rules:\n  - {id: engine.x, effect: allow, when: {tool: x}}", "reserved"),
        ("rules:\n  - {id: default-deny, effect: allow, when: {tool: x}}", "reserved"),
        ("rules:\n  - {id: a, effect: allow, when: {args: {p: {bogus: 1}}}}", "unknown operator"),
        ("rules:\n  - {id: a, effect: allow, when: {args: {p: {gt: high}}}}", "needs a number"),
        ("rules:\n  - {id: a, effect: allow, when: {args: {p: {in: 5}}}}", "needs a list"),
        ("rules:\n  - {id: a, effect: allow, when: {args: {p: {exists: 1}}}}", "true or false"),
        ("rules:\n  - {id: a, effect: allow, when: {args: {p: {domain_in: [1]}}}}", "domain"),
        ("rules:\n  - {id: a, effect: allow, when: {args: {p: {}}}}", "no operators"),
        ("rules:\n  - {id: a, effect: allow, when: {args: {p: {eq: [1]}}}}", "scalar"),
        ("rules:\n  - {id: a, effect: allow, when: {args: {p: {bogus: $v}}}}", "unknown operator"),
        (VALID + "  - {id: a, effect: deny, when: {tool: z}}", "duplicate rule id"),
    ],
)
def test_invalid_policies_fail_at_load(text: str, fragment: str) -> None:
    with pytest.raises(RuleConfigError, match=fragment):
        parse_rules(text)


def test_variable_operands_pass_load_validation() -> None:
    parse_rules("rules:\n  - {id: a, effect: allow, when: {args: {p: {gt: $limits.max}}}}")


def test_empty_rule_list_is_allowed() -> None:
    assert parse_rules("rules: []") == ()


def test_apply_operator_matches_like_a_rule_and_checks_its_operand() -> None:
    assert apply_operator("domain_in", "A@Evil.Example", ["evil.example"])
    assert not apply_operator("eq", 3, 4) and apply_operator("lte", 3, 4)
    with pytest.raises(ValueError):
        apply_operator("near", 1, 2)
    with pytest.raises(ValueError):
        apply_operator("in", "a", "not a list")
