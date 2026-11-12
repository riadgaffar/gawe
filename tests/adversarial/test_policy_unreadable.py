"""Unreadable inputs cannot authorize actions under any shipped policy.

This is an engine property, independent of individual rule boundary tests. Plain free text
without a policy parser is not classified as unreadable. Generated malformed addresses are
invalid by construction; the production address parser is never used as the test oracle.
"""

import fnmatch
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from gawe.apps import contract_review as cr
from gawe.apps import it_access as ia
from gawe.core.policy import rules as rules_module
from gawe.core.policy.engine import RuleEngine
from gawe.core.policy.rules import (
    MISSING,
    RuleConfigError,
    apply_operator,
    load_rules,
    parse_rules,
)
from tests.helpers import TOOLS, make_ctx, signer

# Expected outcomes are independently specified, not computed from the policy being tested.
CASES = {
    "contract_review": {
        "clm.get_contract": ({"contract_id": "C-1001"}, "allow"),
        "crm.write_assessment": (
            {"contract_id": "C-1001", "risk_level": "low", "summary": "Review"},
            "allow",
        ),
        "email.send": ({"to": "a@acme.example", "subject": "Review", "body": "Hello"}, "allow"),
        "esign.send_for_signature": (
            {"contract_id": "C-1001", "deal_value": 100, "signer_email": "a@acme.example"},
            "needs_approval",
        ),
    },
    "it_access": {
        "iam.lookup_user": ({"email": "a@acme.example"}, "allow"),
        "iam.grant_access": (
            {"email": "a@acme.example", "system": "wiki", "level": "read"},
            "allow",
        ),
    },
    "example": {
        "contracts.extract_risks": ({"document_id": "doc-1"}, "allow"),
        "contracts.send_email": ({"to": "a@acme.com", "body": "Hello"}, "allow"),
        "crm.write_assessment": ({"contract_id": "C-1001", "risk_score": 1}, "allow"),
        "crm.mark_approved_for_signature": (
            {"contract_id": "C-1001", "deal_value": 100},
            "needs_approval",
        ),
        "esign.send_for_signature": ({"contract_id": "C-1001"}, "deny"),
    },
}
MANIFESTS = {
    "contract_review": {m.name: m for m in cr.MANIFESTS},
    "it_access": {m.name: m for m in ia.MANIFESTS},
    "example": TOOLS,
}
PATHS = {
    "contract_review": cr.HERE / "policies.yaml",
    "it_access": ia.HERE / "policies.yaml",
    "example": Path(__file__).parents[1] / "fixtures" / "example_policies.yaml",
}
VARIABLES = {
    "contract_review": {
        **cr.TENANT_VARIABLES,
        "review": {"unsupported_levels": [], "large_deals": []},
    },
    "it_access": {**ia.TENANT_VARIABLES, "request": {"requester_email": "a@acme.example"}},
    "example": {"tenant": {"internal_domains": ["acme.com"]}},
}
RULES = {
    app: load_rules(path, manifests=tuple(MANIFESTS[app].values())) for app, path in PATHS.items()
}
ENGINES = {app: RuleEngine(rules, signer()) for app, rules in RULES.items()}
DOMAIN_TARGETS = {
    ("contract_review", "email.send", "to"): ("needs_approval", "external-email-needs-legal"),
    ("it_access", "iam.grant_access", "email"): ("deny", "external-user-denied"),
    ("example", "contracts.send_email", "to"): ("needs_approval", "external-email-needs-legal"),
}
VARIABLE_TARGETS = [
    ("contract_review", "email.send", "tenant.internal_domains"),
    ("contract_review", "email.send", "tenant.blocked_domains"),
    ("contract_review", "crm.write_assessment", "review.unsupported_levels"),
    ("contract_review", "crm.write_assessment", "review.large_deals"),
    ("it_access", "iam.grant_access", "tenant.internal_domains"),
    ("it_access", "iam.grant_access", "request.requester_email"),
    ("example", "contracts.send_email", "tenant.internal_domains"),
]
# Every field and mutation kind is a pytest parameter: random selection cannot omit a target.
TARGETS = (
    [
        (app, name, field, kind)
        for app, cases in CASES.items()
        for name, (args, _) in cases.items()
        for field in args
        for kind in ("missing", "null", "wrong_type")
    ]
    + [(*target, "domain") for target in DOMAIN_TARGETS]
    + [
        (*target, kind)
        for target in VARIABLE_TARGETS
        for kind in ("variable_missing", "variable_type")
    ]
)
JSON_SCALARS = st.none() | st.booleans() | st.integers(-1000, 1000) | st.text(max_size=12)
JSON_VALUES = st.recursive(
    JSON_SCALARS,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=8), children, max_size=3)
    ),
    max_leaves=8,
)
WRONG_TYPES = (
    st.booleans()
    | st.lists(JSON_VALUES, max_size=3)
    | st.dictionaries(st.text(max_size=8), JSON_VALUES, max_size=3)
)
# Restricted ASCII tokens keep size/schema validity independent of the domain parser.
TOKEN = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=12)


def decide(app: str, name: str, args: dict[str, Any], variables: dict[str, Any]) -> Any:
    manifest = MANIFESTS[app][name]
    decision = ENGINES[app].evaluate(
        make_ctx(manifest, args, roles=manifest.required_roles, variables=variables)
    )
    assert signer().verify(decision)
    return decision


@pytest.mark.parametrize("app,name,field,kind", TARGETS)
@settings(max_examples=40, derandomize=True, database=None, deadline=None)
@given(data=st.data())
def test_unreadable_input_never_allows(
    app: str, name: str, field: str, kind: str, data: st.DataObject
) -> None:
    args = deepcopy(CASES[app][name][0])
    variables = deepcopy(VARIABLES[app])
    # Exercise both populated and empty runtime lists without mislabelling an empty positive
    # membership list as unreadable. Empty review lists are a legitimate policy choice.
    if data.draw(st.booleans(), label="empty_internal_domains"):
        variables["tenant"]["internal_domains"] = []
    if app == "contract_review":
        variables["tenant"]["blocked_domains"] = data.draw(st.sampled_from([[], ["evil.example"]]))
        variables["review"] = {
            "unsupported_levels": data.draw(st.sampled_from([[], ["low"]])),
            "large_deals": data.draw(st.sampled_from([[], ["C-1001"]])),
        }
    if kind.startswith("variable_"):
        parent, key = field.split(".")
        if kind == "variable_missing":
            del variables[parent][key]
        else:
            variables[parent][key] = (
                [] if parent == "request" else data.draw(st.none() | st.booleans() | TOKEN)
            )
        # Templates satisfy preceding conjuncts, including risk_level == low.
        decision = decide(app, name, args, variables)
        assert decision.outcome == "deny"
        assert decision.rule_ids == ("engine.error",)
    elif kind == "domain":
        token = data.draw(TOKEN)
        args[field] = data.draw(
            st.sampled_from(
                [
                    f"{token}@@acme.example",
                    f"{token}@acme.example,other@evil.example",
                    f" {token}@acme.example",
                    f"{token}@acme.example ",
                    f"{token}@acme.example;other@evil.example",
                ]
            )
        )
        assert Draft202012Validator(MANIFESTS[app][name].input_schema).is_valid(args)
        outcome, rule_id = DOMAIN_TARGETS[(app, name, field)]
        decision = decide(app, name, args, variables)
        assert decision.outcome == outcome
        assert rule_id in decision.rule_ids
    else:
        if kind == "missing":
            del args[field]
        else:
            args[field] = None if kind == "null" else data.draw(WRONG_TYPES)
        assert not Draft202012Validator(MANIFESTS[app][name].input_schema).is_valid(args)
        decision = decide(app, name, args, variables)
        assert decision.outcome == "deny"
        assert decision.rule_ids == ("engine.invalid_args",)
    assert decision.outcome != "allow"


@pytest.mark.parametrize("app,name", [(app, name) for app in CASES for name in CASES[app]])
def test_readable_controls_keep_expected_outcomes(app: str, name: str) -> None:
    args, expected = CASES[app][name]
    assert Draft202012Validator(MANIFESTS[app][name].input_schema).is_valid(args)
    assert decide(app, name, args, deepcopy(VARIABLES[app])).outcome == expected


def test_property_targets_cover_all_shipped_tools_paths_and_variables() -> None:
    discovered = set((Path(cr.__file__).parents[1]).glob("*/policies.yaml"))
    assert discovered == {PATHS["contract_review"], PATHS["it_access"]}
    domains = set()
    variables = set()
    for app, manifests in MANIFESTS.items():
        assert set(CASES[app]) == set(manifests)
        for name, manifest in manifests.items():
            fields = set(CASES[app][name][0])
            assert fields == set(manifest.input_schema["properties"])
            for rule in RULES[app]:
                when = rule.when
                if when.tool is not None and not fnmatch.fnmatchcase(name, when.tool):
                    continue
                if (
                    when.side_effect_class is not None
                    and when.side_effect_class != manifest.side_effect_class
                ):
                    continue
                if when.risk_tier is not None and when.risk_tier != manifest.risk_tier:
                    continue
                assert set(when.args) <= fields  # A future nested path needs an explicit strategy.
                for path, conditions in when.args.items():
                    for op, operand in conditions.items():
                        if op in {"domain_in", "domain_not_in"}:
                            domains.add((app, name, path))
                        if isinstance(operand, str) and operand.startswith("$"):
                            variables.add((app, name, operand[1:]))
    assert domains == set(DOMAIN_TARGETS)
    assert variables == set(VARIABLE_TARGETS)


# --- Rule layer, with the schema gate removed ---------------------------------------------------
# Under shipped policies the schema check stops missing, null and wrong-type input before any
# rule runs, so the tests above would not notice a change to how rules treat unreadable values.
# The tests below call the rule layer directly.

# Written by hand, not read from rules.py: whether a check holds when its value is unreadable.
# Negative checks hold (they restrict); positive ones do not. `exists` depends on its operand.
HOLDS_WHEN_UNREADABLE = {
    "eq": False,
    "in": False,
    "gt": False,
    "gte": False,
    "lt": False,
    "lte": False,
    "domain_in": False,
    "ne": True,
    "not_in": True,
    "domain_not_in": True,
}
NUMERIC_OPS = {"gt", "gte", "lt", "lte"}
DOMAIN_OPS = {"domain_in", "domain_not_in"}
ABSENT = object()  # drop the key from the arguments
SCALARS = TOKEN | st.integers(-1000, 1000)
CONTAINERS = st.lists(JSON_SCALARS, max_size=3) | st.dictionaries(
    st.text(max_size=4), JSON_SCALARS, max_size=2
)
MALFORMED_ADDRESSES = TOKEN.flatmap(
    lambda t: st.sampled_from(
        [
            t,
            f"{t}@",
            f"@{t}.example",
            f"{t}@@acme.example",
            f"{t}@acme.example,other@evil.example",
            f"{t}@acme.example;other@evil.example",
            f" {t}@acme.example",
            f"Name <{t}@acme.example>",
        ]
    )
)


def unreadable_values(op: str) -> st.SearchStrategy[Any]:
    if op == "exists":
        return st.just(ABSENT)
    values = st.just(ABSENT) | CONTAINERS
    if op in NUMERIC_OPS:
        values |= st.none() | st.booleans() | TOKEN
    if op in DOMAIN_OPS:
        values |= st.none() | st.booleans() | st.integers(-1000, 1000) | MALFORMED_ADDRESSES
    return values


def operands(op: str) -> st.SearchStrategy[Any]:
    if op in NUMERIC_OPS:
        return st.integers(-1000, 1000)
    if op in {"eq", "ne"}:
        return SCALARS
    if op in {"in", "not_in"}:
        return st.lists(SCALARS, max_size=3)  # includes the empty list
    if op in DOMAIN_OPS:
        return st.lists(TOKEN.map(lambda t: f"{t}.example"), max_size=3)
    return st.booleans()


def expected_hold(op: str, operand: Any) -> bool:
    return operand is False if op == "exists" else HOLDS_WHEN_UNREADABLE[op]


def place(path: str, value: Any) -> dict[str, Any]:
    *parents, leaf = path.split(".")
    args: dict[str, Any] = {} if value is ABSENT else {leaf: value}
    for parent in reversed(parents):
        args = {parent: args}
    return args


def test_unreadable_semantics_cover_every_operator() -> None:
    assert {*HOLDS_WHEN_UNREADABLE, "exists"} == set(rules_module._OPS)


@pytest.mark.parametrize("op", sorted(rules_module._OPS))
@settings(max_examples=60, derandomize=True, database=None, deadline=None)
@given(data=st.data())
def test_every_operator_restricts_on_unreadable_values(op: str, data: st.DataObject) -> None:
    operand = data.draw(operands(op), label="operand")
    value = data.draw(unreadable_values(op), label="value")
    held = apply_operator(op, MISSING if value is ABSENT else value, operand)
    assert held is expected_hold(op, operand)


PERMISSIVE = TOOLS["contracts.send_email"].model_copy(update={"input_schema": {"type": "object"}})


@pytest.mark.parametrize("path", ["x", "x.y"])
@pytest.mark.parametrize("op", sorted(rules_module._OPS))
@settings(max_examples=40, derandomize=True, database=None, deadline=None)
@given(data=st.data())
def test_no_loadable_allow_rule_allows_unreadable_input(
    path: str, op: str, data: st.DataObject
) -> None:
    operand = data.draw(operands(op), label="operand")
    as_variable = data.draw(st.booleans(), label="as_variable")
    rule = {
        "id": "candidate",
        "effect": "allow",
        "when": {"tool": PERMISSIVE.name, "args": {path: {op: "$v" if as_variable else operand}}},
    }
    try:
        rules = parse_rules(yaml.safe_dump({"rules": [rule]}))
    except RuleConfigError:
        return  # refused at load: it can never decide anything
    value = data.draw(unreadable_values(op), label="value")
    decision = RuleEngine(rules, signer()).evaluate(
        make_ctx(
            PERMISSIVE,
            place(path, value),
            roles=PERMISSIVE.required_roles,
            variables={"v": operand},
        )
    )
    assert decision.outcome != "allow"


def resolve(operand: Any, variables: dict[str, Any]) -> Any:
    if not (isinstance(operand, str) and operand.startswith("$")):
        return operand
    current: Any = variables
    for part in operand[1:].split("."):
        current = current[part]
    return current


def emptied(variables: dict[str, Any]) -> dict[str, Any]:
    return {
        key: emptied(value) if isinstance(value, dict) else [] if isinstance(value, list) else value
        for key, value in variables.items()
    }


SHIPPED_PREDICATES = [
    (app, rule.id, rule.effect, path, op, operand)
    for app, rules in RULES.items()
    for rule in rules
    for path, conditions in rule.when.args.items()
    for op, operand in conditions.items()
]
POPULATED = {
    **VARIABLES,
    "contract_review": {
        **VARIABLES["contract_review"],
        "review": {"unsupported_levels": ["low"], "large_deals": ["C-1001"]},
    },
}


@pytest.mark.parametrize(
    "app,rule_id,effect,path,op,operand",
    SHIPPED_PREDICATES,
    ids=[f"{p[0]}-{p[1]}-{p[3]}-{p[4]}" for p in SHIPPED_PREDICATES],
)
@settings(max_examples=30, derandomize=True, database=None, deadline=None)
@given(data=st.data())
def test_shipped_checks_hold_or_fail_toward_restriction_on_unreadable_values(
    app: str, rule_id: str, effect: str, path: str, op: str, operand: Any, data: st.DataObject
) -> None:
    variables = data.draw(st.sampled_from([POPULATED[app], emptied(POPULATED[app])]))
    resolved = resolve(operand, variables)
    value = data.draw(unreadable_values(op), label="value")
    held = apply_operator(op, MISSING if value is ABSENT else value, resolved)
    assert held is expected_hold(op, resolved)
    # An allow predicate that holds on an unreadable value would widen what is allowed.
    assert not (effect == "allow" and held)
