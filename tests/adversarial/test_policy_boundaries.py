"""Independent per-rule boundaries: labels and simple arithmetic are the oracle.

Production parsing is called only to measure the actual parse result. Neither its return value
nor the production operators/matchers are used to choose expected outcomes or rule ids.
"""

from copy import deepcopy
from typing import Any

import pytest
import yaml
from hypothesis import given, seed, settings
from hypothesis import strategies as st

from gawe.apps.contract_review import review_variables
from gawe.core.policy.rules import RuleConfigError, email_domain, parse_rules
from tests.adversarial.test_policy_unreadable import CASES, ENGINES, MANIFESTS, RULES, VARIABLES
from tests.helpers import make_ctx, signer

# One sentence for each rule, grouped by the generator that exercises it.
BOUNDARIES = {
    "contract_review": {
        "read-tools-allowed": ("baseline", "Read tools have baseline permission."),
        "crm-assessment-allowed": ("assessment", "CRM assessment writes have baseline permission."),
        "email-baseline-allowed": ("address", "Email has baseline permission."),
        "external-email-needs-legal": (
            "address",
            "Noninternal or unreadable recipients need legal.",
        ),
        "blocked-domain-email-denied": ("address", "A blocked recipient domain denies."),
        "low-risk-contradicted-needs-legal": ("assessment", "Unsupported levels need legal."),
        "low-risk-large-deal-needs-legal": (
            "assessment",
            "Low on a listed large deal needs legal.",
        ),
        "signature-standard-needs-legal": ("signature", "Values at or below 100,000 need legal."),
        "signature-high-value-needs-finance": ("signature", "Values above 100,000 need finance."),
    },
    "it_access": {
        "lookup-allowed": ("baseline", "Read tools have baseline permission."),
        "grant-baseline-allowed": ("grant", "Grants have baseline permission."),
        "requester-mismatch-denied": ("requester", "A different reported requester denies."),
        "elevated-access-needs-security": ("grant", "Admin access needs security."),
        "production-database-needs-security": ("grant", "Production DB access needs security."),
        "external-user-denied": ("address", "Noninternal or unreadable grant recipients deny."),
    },
    "example": {
        "read-tools-allowed": ("baseline", "Read tools have baseline permission."),
        "crm-write-allowed": ("baseline", "CRM assessment writes have baseline permission."),
        "email-baseline-allowed": ("address", "Email has baseline permission."),
        "external-email-needs-legal": (
            "address",
            "Noninternal or unreadable recipients need legal.",
        ),
        "deny-competitor-email": ("address", "The competitor domain denies."),
        "signature-standard-needs-legal": ("signature", "Values at or below 100,000 need legal."),
        "signature-high-value-needs-finance": ("signature", "Values above 100,000 need finance."),
    },
}
FIXED_SEED = 15001
GENERATED = settings(max_examples=30, derandomize=True, database=None, deadline=None)
TOKEN = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=10)


def assert_decision(
    app: str,
    tool: str,
    args: dict[str, Any],
    variables: dict[str, Any],
    outcome: str,
    ids: set[str],
    *,
    roles: tuple[str, ...] | None = None,
    parse: tuple[Any, bool] | None = None,
) -> None:
    if parse is not None:
        value, expected_parse = parse
        assert (email_domain(value) is not None) is expected_parse
    manifest = MANIFESTS[app][tool]
    decision = ENGINES[app].evaluate(
        make_ctx(
            manifest,
            args,
            roles=manifest.required_roles if roles is None else roles,
            variables=variables,
        )
    )
    assert decision.outcome == outcome
    assert set(decision.rule_ids) == ids
    assert signer().verify(decision)


# kind -> parser label, policy domain category. Malformed kinds are not classified by parsing.
ADDRESS_KINDS = {
    "internal": (True, "internal"),
    "plus": (True, "internal"),
    "uppercase": (True, "internal"),
    "unicode_local": (True, "internal"),
    "idn_unicode": (True, "internal"),
    "idn_ascii": (True, "internal"),
    "external_idn": (True, "external"),
    "lookalike": (True, "external"),
    "subdomain": (True, "external"),
    "suffix": (True, "external"),
    "blocked": (True, "blocked"),
    "blocked_internal": (True, "blocked_internal"),
    "comma": (False, "external"),
    "semicolon": (False, "external"),
    "display": (False, "external"),
    "quoted": (False, "external"),
    "newline": (False, "external"),
    "control": (False, "external"),
    "format_control": (False, "external"),
    "space": (False, "external"),
    "trailing_dot": (False, "external"),
    "empty_label": (False, "external"),
    "leading_hyphen": (False, "external"),
    "trailing_hyphen": (False, "external"),
    "invalid_alabel": (False, "external"),
    "fullwidth": (False, "external"),
    "unicode_separator": (False, "external"),
}


@pytest.mark.parametrize("app", ["contract_review", "it_access", "example"])
@pytest.mark.parametrize("kind", ADDRESS_KINDS)
@seed(FIXED_SEED)
@GENERATED
@given(token=TOKEN)
def test_address_boundaries(app: str, kind: str, token: str) -> None:
    tool = {
        "contract_review": "email.send",
        "it_access": "iam.grant_access",
        "example": "contracts.send_email",
    }[app]
    field = "email" if app == "it_access" else "to"
    args = deepcopy(CASES[app][tool][0])
    variables = deepcopy(VARIABLES[app])
    internal = "acme.com" if app == "example" else "acme.example"
    blocked = "competitor.com" if app == "example" else "evil.example"
    address = f"{token}@{internal}"
    addresses = {
        "internal": address,
        "plus": f"{token}+tag@{internal}",
        "uppercase": f"{token.upper()}@{internal.upper()}",
        "unicode_local": f"élève+{token}@{internal}",
        "idn_unicode": f"{token}@bücher.example",
        "idn_ascii": f"{token}@xn--bcher-kva.example",
        "external_idn": f"{token}@bücher.example",
        "lookalike": f"{token}@аcme.example",
        "subdomain": f"{token}@sub.{internal}",
        "suffix": f"{address}.evil.example",
        "blocked": f"{token}@{blocked}",
        "blocked_internal": f"{token}@{blocked}",
        "comma": f"{address},other@evil.example",
        "semicolon": f"{address};other@evil.example",
        "display": f"Name <{address}>",
        "quoted": f'"{token}"@{internal}',
        "newline": address + "\n",
        "control": token + "\x00@" + internal,
        "format_control": token + "\u200b@" + internal,
        "space": address + " ",
        "trailing_dot": address + ".",
        "empty_label": f"{token}@a..example",
        "leading_hyphen": f"{token}@a.-label.example",
        "trailing_hyphen": f"{token}@label-.example",
        "invalid_alabel": f"{token}@xn--.example",
        "fullwidth": f"{token}@ａｃｍｅ.example",
        "unicode_separator": f"{token}@acme。example",
    }
    args[field] = addresses[kind]
    if kind == "idn_unicode":
        variables["tenant"]["internal_domains"] = ["XN--BCHER-KVA.EXAMPLE"]
    elif kind == "idn_ascii":
        variables["tenant"]["internal_domains"] = ["bücher.example"]
    elif kind == "blocked_internal":
        variables["tenant"]["internal_domains"] = [blocked]
    parses, category = ADDRESS_KINDS[kind]
    internal_match = category in {"internal", "blocked_internal"}
    if app == "it_access" and parses:
        # Isolate the domain boundary with a separately declared matching Reader report.
        variables["request"]["requester_email"] = addresses[kind]
    if app == "it_access":
        expected = "allow" if internal_match else "deny"
        ids = {"grant-baseline-allowed"} | (set() if internal_match else {"external-user-denied"})
    else:
        expected = "allow" if internal_match else "needs_approval"
        ids = {"email-baseline-allowed"}
        if not internal_match:
            ids.add("external-email-needs-legal")
        if category in {"blocked", "blocked_internal"}:
            expected = "deny"
            ids.add("deny-competitor-email" if app == "example" else "blocked-domain-email-denied")
    if app == "it_access" and not parses:
        ids.add("requester-mismatch-denied")
    assert_decision(app, tool, args, variables, expected, ids, parse=(args[field], parses))


@pytest.mark.parametrize(
    "app,tool",
    [
        ("contract_review", "esign.send_for_signature"),
        ("example", "crm.mark_approved_for_signature"),
    ],
)
@pytest.mark.parametrize("side", ["below", "equal", "above"])
@seed(FIXED_SEED)
@GENERATED
@given(delta=st.integers(min_value=1, max_value=100_000))
def test_signature_threshold_boundaries(app: str, tool: str, side: str, delta: int) -> None:
    args = deepcopy(CASES[app][tool][0])
    args["deal_value"] = 100_000 + {"below": -delta / 100, "equal": 0, "above": delta / 100}[side]
    rule = (
        "signature-high-value-needs-finance"
        if side == "above"
        else "signature-standard-needs-legal"
    )
    assert_decision(app, tool, args, deepcopy(VARIABLES[app]), "needs_approval", {rule})


@pytest.mark.parametrize("level", ["low", "medium", "high"])
@pytest.mark.parametrize("unsupported", [False, True])
@pytest.mark.parametrize("large", [False, True])
@seed(FIXED_SEED)
@GENERATED
@given(contract=st.integers(1000, 9999))
def test_assessment_boundaries(level: str, unsupported: bool, large: bool, contract: int) -> None:
    cid = f"C-{contract}"
    variables = deepcopy(VARIABLES["contract_review"])
    variables["review"] = {
        "unsupported_levels": [level] if unsupported else [],
        "large_deals": [cid] if large else [],
    }
    ids = {"crm-assessment-allowed"}
    if unsupported:
        ids.add("low-risk-contradicted-needs-legal")
    if large and level == "low":
        ids.add("low-risk-large-deal-needs-legal")
    assert_decision(
        "contract_review",
        "crm.write_assessment",
        {"contract_id": cid, "risk_level": level, "summary": "Review"},
        variables,
        "needs_approval" if len(ids) > 1 else "allow",
        ids,
    )


@pytest.mark.parametrize("system", ["wiki", "crm", "billing", "prod_db"])
@pytest.mark.parametrize("level", ["read", "write", "admin"])
@pytest.mark.parametrize("external", [False, True])
@seed(FIXED_SEED)
@GENERATED
@given(token=TOKEN)
def test_grant_boundaries(system: str, level: str, external: bool, token: str) -> None:
    email = token + ("@outside.example" if external else "@acme.example")
    ids = {"grant-baseline-allowed"}
    if system == "prod_db":
        ids.add("production-database-needs-security")
    if level == "admin":
        ids.add("elevated-access-needs-security")
    if external:
        ids.add("external-user-denied")
    outcome = "deny" if external else "needs_approval" if len(ids) > 1 else "allow"
    assert_decision(
        "it_access",
        "iam.grant_access",
        {"email": email, "system": system, "level": level},
        {**deepcopy(VARIABLES["it_access"]), "request": {"requester_email": email}},
        outcome,
        ids,
        parse=(email, True),
    )


@pytest.mark.parametrize(
    "app,tool,ids,outcome",
    [
        ("contract_review", "clm.get_contract", {"read-tools-allowed"}, "allow"),
        ("it_access", "iam.lookup_user", {"lookup-allowed"}, "allow"),
        ("example", "contracts.extract_risks", {"read-tools-allowed"}, "allow"),
        ("example", "crm.write_assessment", {"crm-write-allowed"}, "allow"),
        ("example", "esign.send_for_signature", {"default-deny"}, "deny"),
    ],
)
@seed(FIXED_SEED)
@GENERATED
@given(token=TOKEN)
def test_tool_selector_boundaries(
    app: str, tool: str, ids: set[str], outcome: str, token: str
) -> None:
    args = deepcopy(CASES[app][tool][0])
    if "document_id" in args:
        args["document_id"] = token
    elif "email" in args:
        args["email"] = token + "@acme.example"
    elif "risk_score" in args:
        args["risk_score"] = len(token)
    assert_decision(app, tool, args, deepcopy(VARIABLES[app]), outcome, ids)


@pytest.mark.parametrize("app", ["contract_review", "it_access", "example"])
@pytest.mark.parametrize("kind", ["missing", "type", "invalid_entry", "empty", "rbac", "schema"])
@seed(FIXED_SEED)
@GENERATED
@given(token=TOKEN)
def test_domain_variable_and_early_gate_boundaries(app: str, kind: str, token: str) -> None:
    tool = {
        "contract_review": "email.send",
        "it_access": "iam.grant_access",
        "example": "contracts.send_email",
    }[app]
    args = deepcopy(CASES[app][tool][0])
    variables = deepcopy(VARIABLES[app])
    field = "email" if app == "it_access" else "to"
    roles = None
    expected, ids = "deny", {"engine.error"}
    if kind == "missing":
        del variables["tenant"]["internal_domains"]
    elif kind == "type":
        variables["tenant"]["internal_domains"] = token
    elif kind == "invalid_entry":
        # Even if one entry matches, an invalid entry must not be dropped.
        variables["tenant"]["internal_domains"].append(token + "..example")
    elif kind == "empty":
        variables["tenant"]["internal_domains"] = []
        expected = "deny" if app == "it_access" else "needs_approval"
        ids = (
            {"grant-baseline-allowed", "external-user-denied"}
            if app == "it_access"
            else {"email-baseline-allowed", "external-email-needs-legal"}
        )
    elif kind == "rbac":
        roles, ids = (), {"rbac.missing_role"}
    else:
        args[field], ids = None, {"engine.invalid_args"}
    assert_decision(
        app,
        tool,
        args,
        variables,
        expected,
        ids,
        roles=roles,
        parse=(args[field], kind != "schema"),
    )


@pytest.mark.parametrize("op", ["domain_in", "domain_not_in"])
@pytest.mark.parametrize("domain", ["", "a..example", "a.example.", "a.example\n", "xn--.example"])
def test_invalid_literal_domain_operands_fail_at_load(op: str, domain: str) -> None:
    with pytest.raises(RuleConfigError, match="valid domain names"):
        parse_rules(
            yaml.safe_dump(
                {
                    "rules": [
                        {
                            "id": "bad",
                            "effect": "deny",
                            "when": {"args": {"to": {op: ["acme.example", domain]}}},
                        }
                    ]
                }
            )
        )


@seed(FIXED_SEED)
@GENERATED
@given(delta=st.integers(1, 1000))
def test_large_deal_source_threshold(delta: int) -> None:
    values = {"below": 100_000 - delta, "equal": 100_000, "above": 100_000 + delta}
    assert review_variables(None, values)["large_deals"] == ["above"]


def test_every_shipped_rule_has_an_independent_boundary() -> None:
    assert set(BOUNDARIES) == set(RULES)
    families = {"baseline", "address", "signature", "assessment", "grant", "requester"}
    for app, rules in RULES.items():
        assert set(BOUNDARIES[app]) == {r.id for r in rules}
        assert all(family in families and sentence for family, sentence in BOUNDARIES[app].values())


@pytest.mark.parametrize(
    "app,tool,field",
    [
        ("contract_review", "esign.send_for_signature", "deal_value"),
        ("example", "crm.mark_approved_for_signature", "deal_value"),
        ("contract_review", "crm.write_assessment", "risk_level"),
        ("it_access", "iam.grant_access", "system"),
        ("it_access", "iam.grant_access", "level"),
    ],
)
@pytest.mark.parametrize("kind", ["absent", "null", "bool", "string", "container"])
@seed(FIXED_SEED)
@GENERATED
@given(token=TOKEN)
def test_numeric_and_enum_invalid_boundaries(
    app: str, tool: str, field: str, kind: str, token: str
) -> None:
    args = deepcopy(CASES[app][tool][0])
    if kind == "absent":
        del args[field]
    else:
        args[field] = {
            "null": None,
            "bool": True,
            "string": "invalid-" + token,
            "container": [token],
        }[kind]
    assert_decision(app, tool, args, deepcopy(VARIABLES[app]), "deny", {"engine.invalid_args"})


@pytest.mark.parametrize("variable", ["unsupported_levels", "large_deals"])
@pytest.mark.parametrize("kind", ["absent", "wrong_type", "nonmatching"])
@seed(FIXED_SEED)
@GENERATED
@given(token=TOKEN)
def test_review_variable_boundaries(variable: str, kind: str, token: str) -> None:
    args = deepcopy(CASES["contract_review"]["crm.write_assessment"][0])
    variables = deepcopy(VARIABLES["contract_review"])
    if kind == "absent":
        del variables["review"][variable]
    else:
        variables["review"][variable] = token if kind == "wrong_type" else ["unrelated-" + token]
    assert_decision(
        "contract_review",
        "crm.write_assessment",
        args,
        variables,
        "allow" if kind == "nonmatching" else "deny",
        {"crm-assessment-allowed"} if kind == "nonmatching" else {"engine.error"},
    )


@pytest.mark.parametrize("app", ["contract_review", "example"])
@seed(FIXED_SEED)
@GENERATED
@given(token=TOKEN)
def test_blocked_idn_operand_is_normalized_without_expanding_internal_list(
    app: str, token: str
) -> None:
    if app == "example":
        # The fixture has a literal competitor list; test its case normalization separately.
        tool, address = "contracts.send_email", token + "@COMPETITOR.COM"
        ids = {"email-baseline-allowed", "external-email-needs-legal", "deny-competitor-email"}
    else:
        tool, address = "email.send", token + "@xn--bcher-kva.example"
        ids = {
            "email-baseline-allowed",
            "external-email-needs-legal",
            "blocked-domain-email-denied",
        }
    args = deepcopy(CASES[app][tool][0])
    variables = deepcopy(VARIABLES[app])
    if app == "contract_review":
        variables["tenant"]["blocked_domains"] = ["bücher.example"]
    args["to"] = address
    assert_decision(app, tool, args, variables, "deny", ids, parse=(address, True))


@pytest.mark.parametrize("kind", ["same", "different", "case", "missing"])
@seed(FIXED_SEED)
@GENERATED
@given(token=TOKEN)
def test_requester_identity_boundary(kind: str, token: str) -> None:
    reported = token + "@acme.example"
    # Proposed identity and report are constructed independently for each boundary label.
    proposed = {
        "same": reported,
        "different": "other-" + token + "@acme.example",
        "case": token + "@ACME.EXAMPLE",
        "missing": reported,
    }[kind]
    variables = deepcopy(VARIABLES["it_access"])
    variables["request"] = {} if kind == "missing" else {"requester_email": reported}
    ids = {"grant-baseline-allowed"}
    if kind in {"different", "case"}:
        ids.add("requester-mismatch-denied")
    elif kind == "missing":
        ids = {"engine.error"}
    assert_decision(
        "it_access",
        "iam.grant_access",
        {"email": proposed, "system": "wiki", "level": "read"},
        variables,
        "allow" if kind == "same" else "deny",
        ids,
        parse=(proposed, True),
    )
