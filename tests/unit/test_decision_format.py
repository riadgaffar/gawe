"""Decision format 2: per-check unreadable markers, the rule-set digest, and v1 compatibility."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from tests.flow import RUN, Flow
from tests.helpers import DIGEST, GOLDEN_V1, KEY, make_ctx, seal_v1, signer, tool
from tests.unit.test_write_readback import BOB, Env

from gawe.api.background import InlineBackground
from gawe.api.wiring import assemble
from gawe.core.audit import verify_chain
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.models import AuditEvent, DecisionCheck, PolicyDecision
from gawe.core.policy.audit_payload import decided_payload
from gawe.core.policy.engine import RuleEngine, policy_digest
from gawe.core.policy.rules import MISSING, parse_rules, unreadable
from gawe.core.policy.signing import ForgedDecisionError
from gawe.core.statemachine import RunStatus, StepStatus
from gawe.llm.fake import FakeProvider

FIXTURES = Path(__file__).parents[1] / "fixtures"


MAIL = tool(
    "mail.send",
    "external",
    "medium",
    {name: {} for name in ("to", "priority", "urgent", "subject", "body")},
)
POLICY = """
rules:
  - id: internal-mail-allowed
    effect: allow
    when:
      tool: mail.send
      args:
        to: {domain_in: [acme.example]}
        priority: {lte: 3}
  - id: urgent-mail-needs-review
    effect: needs_approval
    approver_role: legal
    when:
      tool: mail.send
      args:
        urgent: {eq: true}
  - id: other-tool
    effect: deny
    when:
      tool: other.tool
"""


def engine(text: str = POLICY) -> RuleEngine:
    return RuleEngine(parse_rules(text), signer())


def decide(args: dict[str, Any], *, rules: str = POLICY, **kw: Any) -> PolicyDecision:
    return engine(rules).evaluate(make_ctx(MAIL, args, **kw))


def checks(decision: PolicyDecision) -> list[tuple[str, str, str, bool]]:
    return [(c.rule_id, c.path, c.op, c.unreadable) for c in decision.checks]


# --- format and parsing --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", GOLDEN_V1)
def test_golden_v1_decisions_still_verify(raw: str) -> None:
    decision = PolicyDecision.model_validate_json(raw)
    assert decision.format == 1 and decision.checks == () and decision.policy_digest is None
    assert signer().verify(decision)
    signer().require_valid(decision, expected_action_hash=decision.action_hash)


@pytest.mark.parametrize("raw", GOLDEN_V1)
def test_reference_v1_sealer_matches_the_recorded_signatures(raw: str) -> None:
    golden = json.loads(raw)
    fields = {k: v for k, v in golden.items() if k != "signature"}
    assert seal_v1(**fields).signature == golden["signature"]


def test_v1_with_checks_or_a_digest_is_invalid() -> None:
    golden = json.loads(GOLDEN_V1[0])
    check = {"rule_id": "r", "path": "to", "op": "eq", "unreadable": False}
    with pytest.raises(ValidationError, match="format 1"):
        PolicyDecision.model_validate({**golden, "checks": [check]})
    with pytest.raises(ValidationError, match="format 1"):
        PolicyDecision.model_validate({**golden, "policy_digest": DIGEST})
    with pytest.raises(ValidationError, match="format 1"):
        PolicyDecision.model_validate({**golden, "format": 1, "policy_digest": DIGEST})


def test_v2_needs_a_digest_and_unknown_formats_fail() -> None:
    golden = json.loads(GOLDEN_V1[0])
    with pytest.raises(ValidationError, match="digest"):
        PolicyDecision.model_validate({**golden, "format": 2})
    with pytest.raises(ValidationError):
        PolicyDecision.model_validate({**golden, "format": 3, "policy_digest": DIGEST})
    with pytest.raises(ValidationError):
        PolicyDecision.model_validate({**golden, "format": 2, "policy_digest": "short"})


def test_engine_issues_only_v2_with_the_rule_set_digest() -> None:
    e = engine()
    decision = e.evaluate(make_ctx(MAIL, {"to": "a@acme.example", "priority": 1}))
    assert decision.format == 2
    assert decision.policy_digest == e.policy_digest == policy_digest(parse_rules(POLICY))
    assert signer().verify(decision)
    changed = POLICY.replace("lte: 3", "lte: 4")
    assert engine(changed).policy_digest != e.policy_digest


# --- tampering ---------------------------------------------------------------------------------


def sealed_v2() -> PolicyDecision:
    decision = decide({"to": "a@acme.example", "priority": 1, "urgent": True})
    assert decision.outcome == "needs_approval" and len(decision.checks) == 3
    return decision


@pytest.mark.parametrize(
    "update",
    [
        {"rule_id": "other"},
        {"path": "subject"},
        {"op": "domain_not_in"},
        {"unreadable": True},
    ],
)
def test_changing_any_check_field_breaks_the_signature(update: dict[str, Any]) -> None:
    decision = sealed_v2()
    altered = list(decision.checks)
    altered[0] = altered[0].model_copy(update=update)
    assert not signer().verify(decision.model_copy(update={"checks": tuple(altered)}))


def test_adding_removing_or_reordering_checks_breaks_the_signature() -> None:
    decision = sealed_v2()
    extra = DecisionCheck(rule_id="x", path="to", op="eq", unreadable=False)
    for forged in (
        decision.checks[1:],
        (*decision.checks, extra),
        tuple(reversed(decision.checks)),
        (),
    ):
        assert not signer().verify(decision.model_copy(update={"checks": forged}))


def test_changing_the_digest_breaks_the_signature() -> None:
    decision = sealed_v2()
    assert not signer().verify(decision.model_copy(update={"policy_digest": "e" * 64}))


def test_v2_relabelled_v1_fails_without_falling_back() -> None:
    decision = sealed_v2()
    stripped = decision.model_copy(update={"format": 1, "checks": (), "policy_digest": None})
    assert not signer().verify(stripped)
    relabelled = decision.model_copy(update={"format": 1})
    assert not signer().verify(relabelled)
    with pytest.raises(ForgedDecisionError):
        signer().require_valid(stripped, expected_action_hash=decision.action_hash)


def test_v1_relabelled_v2_fails() -> None:
    golden = PolicyDecision.model_validate_json(GOLDEN_V1[0])
    assert not signer().verify(golden.model_copy(update={"format": 2, "policy_digest": DIGEST}))


def test_v2_signature_does_not_verify_under_the_v1_body() -> None:
    decision = sealed_v2()
    as_v1 = seal_v1(
        step_id=decision.step_id,
        action_hash=decision.action_hash,
        outcome=decision.outcome,
        rule_ids=decision.rule_ids,
        reason=decision.reason,
        approver_role=decision.approver_role,
    )
    assert as_v1.signature != decision.signature


# --- what is recorded --------------------------------------------------------------------------


def test_every_evaluated_check_of_every_selected_rule_in_order() -> None:
    decision = decide({"to": "a@acme.example", "priority": 1, "urgent": True})
    assert checks(decision) == [
        ("internal-mail-allowed", "to", "domain_in", False),
        ("internal-mail-allowed", "priority", "lte", False),
        ("urgent-mail-needs-review", "urgent", "eq", False),
    ]  # other-tool's selector does not match, so it has no checks


def test_a_failed_check_stops_its_rule_and_later_checks_are_absent() -> None:
    decision = decide({"to": "a@outside.example", "priority": 1})
    assert decision.rule_ids == ("default-deny",)
    assert checks(decision) == [
        ("internal-mail-allowed", "to", "domain_in", False),
        ("urgent-mail-needs-review", "urgent", "eq", True),
    ]  # priority was never evaluated, so it is absent rather than shown as readable


def test_unreadable_address_explains_a_default_deny() -> None:
    decision = decide({"to": "a@acme.example, b@evil.example", "priority": 1})
    assert decision.outcome == "deny" and decision.rule_ids == ("default-deny",)
    assert checks(decision)[0] == ("internal-mail-allowed", "to", "domain_in", True)


def test_rbac_and_schema_rejections_carry_no_checks() -> None:
    guarded = MAIL.model_copy(update={"required_roles": ("mailer",)})
    rbac = engine().evaluate(make_ctx(guarded, {"to": "a@acme.example"}))
    assert rbac.rule_ids == ("rbac.missing_role",) and rbac.checks == ()
    strict = MAIL.model_copy(
        update={"input_schema": {"type": "object", "additionalProperties": False}}
    )
    schema = engine().evaluate(make_ctx(strict, {"to": "a@acme.example"}))
    assert schema.rule_ids == ("engine.invalid_args",) and schema.checks == ()
    assert rbac.format == schema.format == 2 and signer().verify(schema)


def test_evaluation_error_records_checks_up_to_the_failing_one() -> None:
    rules = """
rules:
  - id: first
    effect: deny
    when: {tool: mail.send, args: {priority: {gt: 5}}}
  - id: list-check
    effect: needs_approval
    approver_role: legal
    when: {tool: mail.send, args: {to: {any_domain_not_in: [acme.example]}}}
  - id: never-reached
    effect: deny
    when: {tool: mail.send, args: {urgent: {eq: true}}}
"""
    decision = decide({"to": ["not an address"], "priority": 1}, rules=rules)
    assert decision.rule_ids == ("engine.error",)
    assert checks(decision) == [
        ("first", "priority", "gt", False),
        ("list-check", "to", "any_domain_not_in", True),
    ]


def test_unresolved_variable_stops_at_that_check() -> None:
    rules = """
rules:
  - id: needs-variable
    effect: deny
    when: {tool: mail.send, args: {to: {domain_in: $tenant.missing}}}
"""
    decision = decide({"to": "a@acme.example"}, rules=rules)
    assert decision.rule_ids == ("engine.error",)
    assert checks(decision) == [("needs-variable", "to", "domain_in", False)]


READABILITY = [
    # (op, value, unreadable)
    ("eq", MISSING, True),
    ("eq", None, False),
    ("eq", True, False),
    ("eq", "x", False),
    ("eq", 3, False),
    ("eq", [1], True),
    ("eq", {"a": 1}, True),
    ("ne", MISSING, True),
    ("ne", [], True),
    ("ne", None, False),
    ("in", MISSING, True),
    ("in", {"a": 1}, True),
    ("in", "x", False),
    ("not_in", [1], True),
    ("not_in", None, False),
    ("gt", MISSING, True),
    ("gt", 1, False),
    ("gte", 1.5, False),
    ("lt", True, True),
    ("lte", "3", True),
    ("lte", None, True),
    ("gt", [1], True),
    ("exists", MISSING, False),
    ("exists", None, False),
    ("domain_in", "a@acme.example", False),
    ("domain_in", MISSING, True),
    ("domain_in", "Name <a@acme.example>", True),
    ("domain_not_in", "a@acme.example;b@evil.example", True),
    ("domain_not_in", 5, True),
    ("any_domain_in", ["a@acme.example", "b@evil.example"], False),
    ("any_domain_in", [], True),
    ("any_domain_in", "a@acme.example", True),
    ("any_domain_not_in", ["a@acme.example", "bad"], True),
    ("any_domain_not_in", MISSING, True),
]


@pytest.mark.parametrize("op,value,expected", READABILITY)
def test_readability_per_operator(op: str, value: Any, expected: bool) -> None:
    assert unreadable(op, value) is expected


def test_readability_table_covers_every_operator() -> None:
    from gawe.core.policy import rules as rules_module

    assert {op for op, _, _ in READABILITY} == set(rules_module._OPS)
    with pytest.raises(ValueError, match="unknown operator"):
        unreadable("matches", "x")


SECRET_VALUE = "zq-secret-value"
SECRET_OPERAND = "zq-secret-operand"


def test_no_value_or_operand_appears_in_checks_or_the_audit_payload() -> None:
    rules = yaml.safe_dump(
        {
            "rules": [
                {
                    "id": "secret-check",
                    "effect": "deny",
                    "when": {"tool": "mail.send", "args": {"subject": {"eq": SECRET_OPERAND}}},
                },
                {
                    "id": "variable-check",
                    "effect": "deny",
                    "when": {"tool": "mail.send", "args": {"body": {"in": "$tenant.words"}}},
                },
            ]
        }
    )
    decision = decide(
        {"subject": SECRET_VALUE, "body": SECRET_VALUE},
        rules=rules,
        variables={"tenant": {"words": [SECRET_OPERAND]}},
    )
    assert len(decision.checks) == 2
    for text in (
        json.dumps([c.model_dump() for c in decision.checks]),
        json.dumps(decided_payload(decision)),
    ):
        assert SECRET_VALUE not in text and SECRET_OPERAND not in text


# --- audit payload -----------------------------------------------------------------------------


def test_v1_payload_keeps_the_pre_m16b_shape() -> None:
    golden = PolicyDecision.model_validate_json(GOLDEN_V1[0])
    assert decided_payload(golden) == {
        "step_id": "send",
        "outcome": "needs_approval",
        "rule_ids": ["esign-needs-legal"],
        "action_hash": "a" * 64,
        "approver_role": "legal",
    }


def test_v2_payload_adds_format_checks_and_digest() -> None:
    decision = sealed_v2()
    payload = decided_payload(decision)
    assert payload["format"] == 2
    assert payload["policy_digest"] == decision.policy_digest
    assert payload["checks"] == [c.model_dump() for c in decision.checks]


def test_recorded_pre_m16b_chain_still_verifies_and_matches_its_decision() -> None:
    data = json.loads((FIXTURES / "audit_chain_pre_m16b.json").read_text())
    events = [AuditEvent.model_validate(e) for e in data["events"]]
    assert verify_chain(events).ok
    decision = PolicyDecision.model_validate(data["decisions"]["write"])
    assert decision.format == 1 and signer().verify(decision)
    (decided,) = [e for e in events if e.type == "policy.decided"]
    assert decided.payload_hash == sha256_hex(canonical_json(decided_payload(decision)))


def test_new_runs_audit_the_v2_payload_built_from_the_stored_decision(tmp_path: Path) -> None:
    e = Env(tmp_path, "esign")
    e.advance()
    step = e.stores.steps.get(e.run_id, "write")
    decision = step.policy_decision
    assert decision is not None and decision.format == 2 and decision.checks
    (decided,) = [ev for ev in e.stores.audit.events(e.run_id) if ev.type == "policy.decided"]
    assert decided.payload_hash == sha256_hex(canonical_json(decided_payload(decision)))
    assert verify_chain(e.stores.audit.events(e.run_id)).ok


# --- API ---------------------------------------------------------------------------------------


def test_api_exposes_checks_as_ids_paths_operators_and_booleans(tmp_path: Path) -> None:
    e = Env(tmp_path, "esign")
    e.advance()
    view = e.service._run_view(e.stores.runs.get(e.run_id))
    (step,) = view.steps
    stored = e.stores.steps.get(e.run_id, "write").policy_decision
    assert stored is not None
    assert [c.model_dump() for c in step.policy_checks] == [c.model_dump() for c in stored.checks]
    for check in step.policy_checks:
        assert set(check.model_dump()) == {"rule_id", "path", "op", "unreadable"}
    text = view.model_dump_json()
    for value in ("vendor@outside.example", "45000", "C-1001"):
        assert value not in text


# --- v1 decisions still authorize --------------------------------------------------------------


def as_v1(decision: PolicyDecision) -> PolicyDecision:
    return seal_v1(
        step_id=decision.step_id,
        action_hash=decision.action_hash,
        outcome=decision.outcome,
        rule_ids=decision.rule_ids,
        reason=decision.reason,
        approver_role=decision.approver_role,
    )


def test_v1_allow_authorizes_execution() -> None:
    flow = Flow()
    args = {"to": "colleague@acme.com", "body": "hello"}
    v2 = flow.judge("contracts.send_email", args)
    assert v2.outcome == "allow"
    v1 = as_v1(v2)
    flow.add_step(v1, "contracts.send_email", args)
    assert flow.executor.execute(RUN, "s1", v1).status is StepStatus.SUCCEEDED
    assert flow.connector.calls


def test_pending_v1_approval_can_be_decided_and_executed() -> None:
    flow = Flow()
    args = {"to": "counsel@partner.com", "body": "redlines attached"}
    v2 = flow.judge("contracts.send_email", args)
    assert v2.outcome == "needs_approval"
    v1 = as_v1(v2)
    flow.add_step(v1, "contracts.send_email", args)
    flow.approve(v1)
    assert flow.executor.execute(RUN, "s1", v1).status is StepStatus.SUCCEEDED
    assert flow.connector.calls


def restart_with_stored(tmp_path: Path, rewrite: Any) -> Env:
    """Pause a run for approval, rewrite its stored decision, then approve on a new service."""
    e = Env(tmp_path, "esign")
    assert e.advance() is RunStatus.PAUSED_FOR_APPROVAL
    step = e.stores.steps.get(e.run_id, "write")
    assert step.policy_decision is not None
    stored = rewrite(step.policy_decision)
    e.stores.steps._steps[(e.run_id, "write")] = step.model_copy(  # type: ignore[attr-defined]
        update={"policy_decision": stored}
    )
    e.stores.runs.update(e.run_id, decisions={"write": stored})
    service = assemble(
        e.bundle,
        stores=e.stores,
        signer=signer(),
        provider=FakeProvider([]),
        background=InlineBackground(),
        clock=e.clock,
    )
    (item,) = service.inbox(BOB)
    service.decide(BOB, item.approval_id, decision="approved", action_hash=item.action_hash)
    return e


def test_run_paused_on_a_v1_decision_completes_after_a_restart(tmp_path: Path) -> None:
    # The stored decision is rewritten as one recorded before M16b would have been.
    e = restart_with_stored(tmp_path, as_v1)
    assert e.stores.runs.get(e.run_id).status is RunStatus.COMPLETED
    assert e.stores.steps.get(e.run_id, "write").status is StepStatus.SUCCEEDED
    assert len(e.calls) == 1


def test_stored_v1_decision_under_another_key_is_refused(tmp_path: Path) -> None:
    def forged(decision: PolicyDecision) -> PolicyDecision:
        fields = decision.model_dump(include={"step_id", "action_hash", "outcome", "reason"})
        return seal_v1(
            **fields,
            rule_ids=decision.rule_ids,
            approver_role=decision.approver_role,
            key=bytes(reversed(KEY)),
        )

    e = restart_with_stored(tmp_path, forged)
    assert e.stores.steps.get(e.run_id, "write").status is not StepStatus.SUCCEEDED
    assert e.calls == []


def test_key_is_the_one_the_golden_fixtures_were_signed_with() -> None:
    assert bytes(range(32)) == KEY
