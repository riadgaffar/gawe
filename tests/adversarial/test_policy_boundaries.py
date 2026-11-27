"""Independent per-rule boundaries: labels and simple arithmetic are the oracle.

Production parsing is called only to measure the actual parse result. Neither its return value
nor the production operators/matchers are used to choose expected outcomes or rule ids.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml
from hypothesis import given, seed, settings
from hypothesis import strategies as st

from gawe.apps.contract_review import review_variables
from gawe.apps.it_access import grant_history
from gawe.core.history import HistoryEntry
from gawe.core.policy.rules import RuleConfigError, email_domain, parse_rules
from gawe.core.runs import RunRecord
from gawe.core.statemachine import StepStatus
from tests.adversarial.test_policy_unreadable import CASES, ENGINES, MANIFESTS, RULES, VARIABLES
from tests.helpers import bound_args, make_ctx, prepared_email, signer

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
        "changed-recipients-need-legal": (
            "refresh",
            "Changed directory snapshots require legal approval.",
        ),
        "blocked-domain-email-denied": ("address", "A blocked recipient domain denies."),
        "low-risk-contradicted-needs-legal": ("assessment", "Unsupported levels need legal."),
        "low-risk-large-deal-needs-legal": (
            "assessment",
            "Low on a listed large deal needs legal.",
        ),
        "low-risk-needs-legal": ("assessment", "Every low rating needs legal (M17)."),
        "remove-rating-needs-legal": ("assessment", "Removing a rating needs legal."),
        "rating-lowered-needs-legal": (
            "assessment",
            "A level below the contract's last rating needs legal (M24).",
        ),
        "signature-standard-needs-legal": ("signature", "Values at or below 100,000 need legal."),
        "signature-high-value-needs-finance": ("signature", "Values above 100,000 need finance."),
    },
    "it_access": {
        "lookup-allowed": ("baseline", "Read tools have baseline permission."),
        "grant-baseline-allowed": ("grant", "Grants have baseline permission."),
        "requester-mismatch-denied": ("requester", "A different reported requester denies."),
        "grant-to-other-than-submitter-needs-security": (
            "requester",
            "A grantee who did not file the ticket needs security.",
        ),
        "untrusted-channel-needs-security": (
            "requester",
            "A ticket not filed through the portal needs security.",
        ),
        "elevated-access-needs-security": ("grant", "Admin access needs security."),
        "production-database-needs-security": ("grant", "Production DB access needs security."),
        "control-system-change-needs-security": (
            "grant",
            "Write or admin on a system holding another app's controls needs security.",
        ),
        "external-user-denied": ("address", "Noninternal or unreadable grant recipients deny."),
        "repeated-grant-needs-security": (
            "grant",
            "A system granted to the person the set number of times in the window needs security.",
        ),
        "grant-lifetime-over-maximum-denied": ("grant", "Lifetimes over the maximum deny."),
    },
    "event_replay": {
        "list-allowed": ("baseline", "Read tools have baseline permission."),
        "replay-baseline-allowed": ("replay", "A replay within every limit runs."),
        "replay-many-events-needs-finance": (
            "replay",
            "More than the automatic count needs finance.",
        ),
        "replay-large-total-needs-finance": (
            "replay",
            "More than the automatic total needs finance.",
        ),
        "replay-several-customers-needs-finance": (
            "replay",
            "More than one customer needs finance.",
        ),
        "replay-too-many-events-denied": ("replay", "More than the approval count denies."),
        "replay-too-large-denied": ("replay", "More than the approval total denies."),
        "replay-over-daily-limit-denied": ("replay", "More than what is left today denies."),
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
    if app == "contract_review" and tool == "email.send":
        args = prepared_email(args)
    manifest = MANIFESTS[app][tool]
    decision = ENGINES[app].evaluate(
        make_ctx(
            manifest,
            bound_args(manifest, args),
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
        # Isolate the domain boundary: the Reader and the ticket both name this address.
        variables["request"] = self_filed(addresses[kind])
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
    if app == "contract_review" and not parses:
        expected, ids = "deny", {"engine.error"}
    if app == "it_access" and not parses:
        # Not the reported requester, and not the ticket's submitter either; deny still wins.
        ids |= {
            "requester-mismatch-denied",
            "grant-to-other-than-submitter-needs-security",
            "untrusted-channel-needs-security",
        }
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


# The levels below each earlier rating, written by hand (high > medium > low).
BELOW = {None: [], "low": [], "medium": ["low"], "high": ["low", "medium"]}


@pytest.mark.parametrize("level", ["low", "medium", "high"])
@pytest.mark.parametrize("unsupported", [False, True])
@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("last", list(BELOW))
@seed(FIXED_SEED)
@GENERATED
@given(contract=st.integers(1000, 9999))
def test_assessment_boundaries(
    level: str, unsupported: bool, large: bool, last: str | None, contract: int
) -> None:
    cid = f"C-{contract}"
    variables = deepcopy(VARIABLES["contract_review"])
    variables["review"] = {
        "unsupported_levels": [level] if unsupported else [],
        "large_deals": [cid] if large else [],
    }
    variables["history"] = {"lower_ratings": BELOW[last]}
    ids = {"crm-assessment-allowed"}
    if level in BELOW[last]:
        ids.add("rating-lowered-needs-legal")
    if unsupported:
        ids.add("low-risk-contradicted-needs-legal")
    if large and level == "low":
        ids.add("low-risk-large-deal-needs-legal")
    if level == "low":
        ids.add("low-risk-needs-legal")
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
@pytest.mark.parametrize("repeated", [(), ("wiki",), ("wiki", "billing", "crm", "prod_db")])
@seed(FIXED_SEED)
@GENERATED
@given(token=TOKEN)
def test_grant_boundaries(
    system: str, level: str, external: bool, repeated: tuple[str, ...], token: str
) -> None:
    email = token + ("@outside.example" if external else "@acme.example")
    ids = {"grant-baseline-allowed"}
    if system in repeated:
        ids.add("repeated-grant-needs-security")
    if system == "prod_db":
        ids.add("production-database-needs-security")
    # M23: the CRM holds deal values; M25: billing holds event replay's events and daily total.
    if system in {"crm", "billing"} and level in {"write", "admin"}:
        ids.add("control-system-change-needs-security")
    if level == "admin":
        ids.add("elevated-access-needs-security")
    if external:
        ids.add("external-user-denied")
    outcome = "deny" if external else "needs_approval" if len(ids) > 1 else "allow"
    assert_decision(
        "it_access",
        "iam.grant_access",
        {"email": email, "system": system, "level": level},
        {
            **deepcopy(VARIABLES["it_access"]),
            "request": self_filed(email),
            "history": {"repeated_systems": list(repeated)},
        },
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


@pytest.mark.parametrize(
    "hours,maximum,outcome",
    [
        (719, 720, "allow"),
        (720, 720, "allow"),
        (720, 719, "deny"),
        (168, 167, "deny"),
        (168, 168, "allow"),
    ],
)
def test_grant_lifetime_boundary(hours: int, maximum: int, outcome: str) -> None:
    args = deepcopy(CASES["it_access"]["iam.grant_access"][0])
    args["lifetime_hours"] = hours
    variables = deepcopy(VARIABLES["it_access"])
    variables["tenant"]["max_grant_lifetime_hours"] = maximum
    ids = {"grant-baseline-allowed"}
    if outcome == "deny":
        ids.add("grant-lifetime-over-maximum-denied")
    assert_decision("it_access", "iam.grant_access", args, variables, outcome, ids)


@pytest.mark.parametrize("hours", [0, 721])
def test_a_lifetime_outside_the_schema_is_denied_before_rules(hours: int) -> None:
    args = deepcopy(CASES["it_access"]["iam.grant_access"][0])
    args["lifetime_hours"] = hours
    variables = deepcopy(VARIABLES["it_access"])
    assert_decision(
        "it_access", "iam.grant_access", args, variables, "deny", {"engine.invalid_args"}
    )


@pytest.mark.parametrize("maximum", ["missing", None, "720", True, [720]])
def test_an_unreadable_grant_maximum_denies(maximum: Any) -> None:
    args = deepcopy(CASES["it_access"]["iam.grant_access"][0])
    variables = deepcopy(VARIABLES["it_access"])
    if maximum == "missing":
        del variables["tenant"]["max_grant_lifetime_hours"]
    else:
        variables["tenant"]["max_grant_lifetime_hours"] = maximum
    assert_decision("it_access", "iam.grant_access", args, variables, "deny", {"engine.error"})


def test_every_shipped_rule_has_an_independent_boundary() -> None:
    assert set(BOUNDARIES) == set(RULES)
    families = {
        "baseline",
        "address",
        "signature",
        "assessment",
        "grant",
        "requester",
        "refresh",
        "replay",
    }
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


@pytest.mark.parametrize(
    "variable", ["review.unsupported_levels", "review.large_deals", "history.lower_ratings"]
)
@pytest.mark.parametrize("kind", ["absent", "wrong_type", "nonmatching"])
@seed(FIXED_SEED)
@GENERATED
@given(token=TOKEN)
def test_review_variable_boundaries(variable: str, kind: str, token: str) -> None:
    # "low", so every assessment variable is read; since M17 a readable "low" still needs legal.
    args = {**deepcopy(CASES["contract_review"]["crm.write_assessment"][0]), "risk_level": "low"}
    variables = deepcopy(VARIABLES["contract_review"])
    parent, key = variable.split(".")
    if kind == "absent":
        del variables[parent][key]
    else:
        variables[parent][key] = token if kind == "wrong_type" else ["unrelated-" + token]
    assert_decision(
        "contract_review",
        "crm.write_assessment",
        args,
        variables,
        "needs_approval" if kind == "nonmatching" else "deny",
        {"crm-assessment-allowed", "low-risk-needs-legal"}
        if kind == "nonmatching"
        else {"engine.error"},
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


def self_filed(address: str, *, channel: str = "portal") -> dict[str, Any]:
    """Request variables for a ticket filed by `address` that the Reader also reports."""
    return {
        "reported_requester": address,
        "submitters": [address],
        "portal_submitters": [address] if channel == "portal" else [],
    }


@pytest.mark.parametrize(
    "kind",
    ["same", "different", "case", "missing", "on_behalf", "fooled_reader", "email_channel"],
)
@seed(FIXED_SEED)
@GENERATED
@given(token=TOKEN)
def test_requester_identity_boundary(kind: str, token: str) -> None:
    reported = token + "@acme.example"
    filer = "manager-" + token + "@acme.example"
    # Proposed grantee, Reader report and ticket submitter are set independently per label.
    proposed = {
        "same": reported,
        "different": "other-" + token + "@acme.example",
        "case": token + "@ACME.EXAMPLE",
        "missing": reported,
        "on_behalf": reported,
        "fooled_reader": reported,
        "email_channel": reported,
    }[kind]
    variables = deepcopy(VARIABLES["it_access"])
    variables["request"] = {
        "missing": {},
        "on_behalf": {  # an honest ticket a manager filed for someone else
            "reported_requester": reported,
            "submitters": [filer],
            "portal_submitters": [filer],
        },
        "fooled_reader": {  # the Reader names the attacker's target; the filer is someone else
            "reported_requester": reported,
            "submitters": [filer],
            "portal_submitters": [filer],
        },
        "email_channel": self_filed(reported, channel="email"),
    }.get(kind, self_filed(reported))
    ids = {"grant-baseline-allowed"}
    outcome = "allow"
    if kind in {"different", "case"}:
        outcome = "deny"
        ids |= {"requester-mismatch-denied"}
        ids |= {"grant-to-other-than-submitter-needs-security", "untrusted-channel-needs-security"}
    elif kind == "missing":
        outcome, ids = "deny", {"engine.error"}
    elif kind in {"on_behalf", "fooled_reader"}:
        outcome = "needs_approval"
        ids |= {"grant-to-other-than-submitter-needs-security", "untrusted-channel-needs-security"}
    elif kind == "email_channel":
        outcome = "needs_approval"
        ids |= {"untrusted-channel-needs-security"}
    assert_decision(
        "it_access",
        "iam.grant_access",
        {"email": proposed, "system": "wiki", "level": "read"},
        variables,
        outcome,
        ids,
        parse=(proposed, True),
    )


def test_changed_recipients_require_legal_even_when_internal() -> None:
    args = prepared_email({"to": "a@acme.example", "subject": "s", "body": "b"})
    args["delivery"]["reapproval_required"] = True
    manifest = MANIFESTS["contract_review"]["email.send"]
    decision = ENGINES["contract_review"].evaluate(
        make_ctx(
            manifest, args, roles=manifest.required_roles, variables=VARIABLES["contract_review"]
        )
    )
    assert decision.outcome == "needs_approval"
    assert set(decision.rule_ids) == {"email-baseline-allowed", "changed-recipients-need-legal"}


# M24: the grant history's window and count, against plain arithmetic. The judged run starts at
# NOW; an earlier grant counts when its run started no more than `days` days before.
NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)


@pytest.mark.parametrize("allowed", [1, 2, 5])
@pytest.mark.parametrize("days", [1, 30])
@pytest.mark.parametrize("earlier", [0, 1, 2, 5, 6])
@pytest.mark.parametrize("offset", [timedelta(0), timedelta(seconds=-1), timedelta(seconds=1)])
def test_grant_history_window_and_count(
    allowed: int, days: int, earlier: int, offset: timedelta
) -> None:
    started = NOW - timedelta(days=days) + offset  # offset > 0 inside, < 0 outside the window
    entries = [
        HistoryEntry(
            run_id=f"r{i}",
            step_id="grant",
            tool_version="2.0.0",
            action_hash="a" * 64,
            status=StepStatus.SUCCEEDED,
            resolved_args={"email": "a@acme.example", "system": "wiki", "level": "read"},
            started_at=started,
        )
        for i in range(earlier)
    ]
    run = RunRecord(
        run_id="judged",
        tenant_id="t",
        acting_user_id="u",
        objective="o",
        profile="it_access",
        created_at=NOW,
    )
    variables = grant_history({"automatic_grants": allowed, "history_days": days})
    inside = offset >= timedelta(0)
    expected = ["wiki"] if inside and earlier >= allowed else []
    assert variables(entries, {}, run) == {"repeated_systems": expected}


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"automatic_grants": 2},
        {"history_days": 30},
        {"automatic_grants": 0, "history_days": 30},
        {"automatic_grants": 2, "history_days": 0},
        {"automatic_grants": True, "history_days": 30},
        {"automatic_grants": "2", "history_days": 30},
        {"automatic_grants": 2.0, "history_days": 30},
    ],
)
def test_unusable_grant_settings_give_no_variable(settings: dict[str, Any]) -> None:
    run = RunRecord(
        run_id="judged",
        tenant_id="t",
        acting_user_id="u",
        objective="o",
        profile="p",
        created_at=NOW,
    )
    assert grant_history(settings)([], {}, run) == {}


# M25: the replay rules, against the shipped settings written out by hand: 10 events and 1,000
# run automatically, 100 and 25,000 with finance, above that denied; one customer, or finance.
@pytest.mark.parametrize("count", [1, 10, 11, 100, 101])
@pytest.mark.parametrize("total", [0, 1_000, 1_001, 25_000, 25_001])
@pytest.mark.parametrize("customers", [1, 2])
@pytest.mark.parametrize("left", ["plenty", "exact", "short"])
def test_replay_boundaries(count: int, total: int, customers: int, left: str) -> None:
    remaining = {"plenty": 10**9, "exact": total, "short": total - 1}[left]
    args = deepcopy(CASES["event_replay"]["billing.replay_events"][0])
    event = args["batch"][0]
    args["batch"] = [
        {
            **event,
            "event_id": f"ev-{i}",
            "delivery_key": f"dk-{i}",
            "customer": f"cus-{i % customers}",
        }
        for i in range(count)
    ]
    args.update(
        event_ids=[e["event_id"] for e in args["batch"]],
        count=count,
        total=total,
        customers=[f"cus-{i}" for i in range(customers)],
        customer_count=customers,
    )
    variables = {**deepcopy(VARIABLES["event_replay"]), "billing": {"remaining_today": remaining}}
    ids = {"replay-baseline-allowed"}
    if count > 10:
        ids.add("replay-many-events-needs-finance")
    if total > 1_000:
        ids.add("replay-large-total-needs-finance")
    if customers > 1:
        ids.add("replay-several-customers-needs-finance")
    denied = set()
    if count > 100:
        denied.add("replay-too-many-events-denied")
    if total > 25_000:
        denied.add("replay-too-large-denied")
    if total > remaining:
        denied.add("replay-over-daily-limit-denied")
    outcome = "deny" if denied else "needs_approval" if len(ids) > 1 else "allow"
    assert_decision("event_replay", "billing.replay_events", args, variables, outcome, ids | denied)


@pytest.mark.parametrize("level", ["none", "low", "medium", "high"])
@pytest.mark.parametrize("last", list(BELOW))
@pytest.mark.parametrize("unsupported", [False, True])
@pytest.mark.parametrize("large", [False, True])
def test_restore_boundaries(level: str, last: str | None, unsupported: bool, large: bool) -> None:
    args = deepcopy(CASES["contract_review"]["crm.restore_assessment"][0])
    args["risk_level"] = level
    variables = deepcopy(VARIABLES["contract_review"])
    variables["review"] = {
        "unsupported_levels": ["low"] if unsupported else [],
        "large_deals": ["C-1001"] if large else [],
    }
    variables["history"] = {"lower_ratings": BELOW[last]}
    ids = {"crm-assessment-allowed"}
    if level == "none":
        ids.add("remove-rating-needs-legal")
    if level == "low":
        ids.add("low-risk-needs-legal")
        if unsupported:
            ids.add("low-risk-contradicted-needs-legal")
        if large:
            ids.add("low-risk-large-deal-needs-legal")
    if level in BELOW[last]:
        ids.add("rating-lowered-needs-legal")
    assert_decision(
        "contract_review",
        "crm.restore_assessment",
        args,
        variables,
        "needs_approval" if len(ids) > 1 else "allow",
        ids,
    )
