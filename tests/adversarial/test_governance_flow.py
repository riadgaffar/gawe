"""Attacks on the governance path. Every test asserts the same thing in the end: the external
system was never called (or, where a legitimate call is expected, it was called exactly once)."""

from datetime import timedelta

import pytest

from gawe.core.approvals import (
    ActionHashMismatchError,
    ApprovalExpiredError,
    ApproverNotAuthorizedError,
    Principal,
    SeparationOfDutiesError,
)
from gawe.core.executor import ExecutionRefusedError
from gawe.core.models import PolicyDecision
from gawe.core.policy.signing import PolicySigner
from gawe.core.statemachine import StepStatus
from tests.flow import ALICE, BOB, EXTERNAL_EMAIL, INTERNAL_EMAIL, RUN, Flow
from tests.helpers import signer

SEND = "contracts.send_email"
H = "f" * 64


def external(flow: Flow) -> PolicyDecision:
    decision = flow.judge(SEND, EXTERNAL_EMAIL)
    assert decision.outcome == "needs_approval"
    flow.add_step(decision, SEND, EXTERNAL_EMAIL)
    return decision


def refused(flow: Flow, decision: PolicyDecision, reason: str, step_id: str | None = None) -> None:
    with pytest.raises(ExecutionRefusedError, match=reason):
        flow.executor.execute(RUN, step_id or decision.step_id, decision)
    assert flow.connector.calls == []
    assert "execution.refused" in flow.audit_types()


def test_external_email_without_approval_is_refused() -> None:
    flow = Flow()
    refused(flow, external(flow), "approval_missing")


def test_approved_external_email_runs_exactly_once() -> None:
    flow = Flow()
    decision = external(flow)
    flow.approve(decision)
    outcome = flow.executor.execute(RUN, "s1", decision)
    assert outcome.status is StepStatus.SUCCEEDED and len(flow.connector.calls) == 1


def test_approval_present_but_step_not_moved_to_approved_is_refused() -> None:
    flow = Flow()
    decision = external(flow)
    record = flow.approvals.request(run_id=RUN, decision=decision, requested_by=ALICE.user_id)
    flow.approvals.decide(
        record.approval_id, decider=BOB, decision="approved", action_hash=decision.action_hash
    )
    refused(flow, decision, "step_not_approved")


def test_approver_who_saw_a_different_action_cannot_approve() -> None:
    flow = Flow()
    decision = external(flow)
    record = flow.approvals.request(run_id=RUN, decision=decision, requested_by=ALICE.user_id)
    with pytest.raises(ActionHashMismatchError):
        flow.approvals.decide(record.approval_id, decider=BOB, decision="approved", action_hash=H)
    refused(flow, decision, "approval_missing")


def test_self_approval_cannot_authorize_execution() -> None:
    flow = Flow()
    decision = external(flow)
    record = flow.approvals.request(run_id=RUN, decision=decision, requested_by=ALICE.user_id)
    both = Principal("alice", frozenset({"legal_reviewer"}))
    with pytest.raises(SeparationOfDutiesError):
        flow.approvals.decide(
            record.approval_id, decider=both, decision="approved", action_hash=decision.action_hash
        )
    refused(flow, decision, "approval_missing")


def test_wrong_role_cannot_approve() -> None:
    flow = Flow()
    decision = external(flow)
    record = flow.approvals.request(run_id=RUN, decision=decision, requested_by=ALICE.user_id)
    with pytest.raises(ApproverNotAuthorizedError):
        flow.approvals.decide(
            record.approval_id,
            decider=Principal("carol", frozenset({"case_manager"})),
            decision="approved",
            action_hash=decision.action_hash,
        )


def test_expired_approval_cannot_be_decided_or_used() -> None:
    flow = Flow()
    decision = external(flow)
    record = flow.approvals.request(run_id=RUN, decision=decision, requested_by=ALICE.user_id)
    flow.clock.advance(hours=2)
    with pytest.raises(ApprovalExpiredError):
        flow.approvals.decide(
            record.approval_id, decider=BOB, decision="approved", action_hash=decision.action_hash
        )
    refused(flow, decision, "approval_missing")


def test_approval_that_expires_before_execution_starts_is_refused() -> None:
    flow = Flow()
    decision = external(flow)
    flow.approve(decision)
    flow.clock.advance(hours=2)
    refused(flow, decision, "approval_missing")
    assert flow.steps.get(RUN, "s1").status is StepStatus.APPROVED  # untouched


def test_rejected_action_never_runs() -> None:
    flow = Flow()
    decision = external(flow)
    record = flow.approvals.request(run_id=RUN, decision=decision, requested_by=ALICE.user_id)
    flow.approvals.decide(
        record.approval_id, decider=BOB, decision="rejected", action_hash=decision.action_hash
    )
    flow.steps.transition(RUN, "s1", expected=StepStatus.AWAITING_APPROVAL, to=StepStatus.REJECTED)
    refused(flow, decision, "approval_missing")


def test_args_changed_after_approval_are_caught_by_stored_hash_check() -> None:
    flow = Flow()
    decision = external(flow)
    flow.approve(decision)
    # an attacker rewrites the stored recipient but leaves the approved hash in place
    tampered = flow.steps.get(RUN, "s1").model_copy(
        update={"resolved_args": {"to": "attacker@evil.test", "body": "x"}}
    )
    flow.steps._steps[(RUN, "s1")] = tampered  # noqa: SLF001
    refused(flow, decision, "stored_hash_drift")


def test_approval_for_one_action_does_not_cover_a_modified_action() -> None:
    flow = Flow()
    original = external(flow)
    flow.approve(original)
    # same step slot re-planned with a different recipient: new hash, new decision, no approval
    other_args = {"to": "someone@elsewhere.com", "body": "redlines attached"}
    drifted = flow.judge(SEND, other_args, step_id="s2")
    assert drifted.action_hash != original.action_hash
    flow.add_step(drifted, SEND, other_args, status=StepStatus.APPROVED)
    refused(flow, drifted, "approval_missing")


def test_old_decision_cannot_be_replayed_for_a_new_action() -> None:
    flow = Flow()
    cheap = flow.judge(SEND, INTERNAL_EMAIL)  # allow
    flow.add_step(flow.judge(SEND, EXTERNAL_EMAIL), SEND, EXTERNAL_EMAIL)
    replay = cheap.model_copy(update={"step_id": "s1"})
    refused(flow, replay, "invalid_policy_decision")


def test_forged_outcome_is_refused() -> None:
    flow = Flow()
    decision = external(flow)
    forged = decision.model_copy(update={"outcome": "allow", "approver_role": None})
    refused(flow, forged, "invalid_policy_decision")


def test_decision_signed_with_another_key_is_refused() -> None:
    flow = Flow()
    decision = external(flow)
    attacker = PolicySigner(bytes(range(1, 33))).seal(
        step_id="s1", action_hash=decision.action_hash, outcome="allow", rule_ids=[], reason="x"
    )
    refused(flow, attacker, "invalid_policy_decision")


def test_decision_for_another_step_is_refused() -> None:
    flow = Flow()
    allowed = flow.judge(SEND, INTERNAL_EMAIL, step_id="s9")
    flow.add_step(flow.judge(SEND, EXTERNAL_EMAIL), SEND, EXTERNAL_EMAIL)
    refused(flow, allowed, "decision_for_other_step", step_id="s1")


def test_denied_action_never_runs_even_with_valid_signature() -> None:
    flow = Flow()
    args = {"to": "ceo@competitor.com", "body": "x"}
    decision = flow.judge(SEND, args)
    assert decision.outcome == "deny"
    flow.add_step(decision, SEND, args, status=StepStatus.POLICY_CHECKED)
    refused(flow, decision, "policy_denied")


def test_unregistered_tool_is_refused_even_with_a_valid_decision() -> None:
    flow = Flow()
    action = signer()
    from gawe.core.canonical import action_hash

    h = action_hash(
        tool="evil.exfiltrate", version="1.0.0", resolved_args={}, run_id=RUN, step_id="s1"
    )
    decision = action.seal(step_id="s1", action_hash=h, outcome="allow", rule_ids=[], reason="x")
    flow.add_step(decision, "evil.exfiltrate", {})
    refused(flow, decision, "tool_not_registered")


@pytest.mark.parametrize(
    "recipient",
    [
        "a@acme.com, attacker@evil.test",  # multi-recipient smuggled behind an internal address
        "Boss <attacker@evil.test>@acme.com",
        "attacker@evil.test;a@acme.com",
        "a@acme.com.evil.test",
        "a@ACME.com\nbcc: attacker@evil.test",
    ],
)
def test_injection_style_recipients_never_auto_send(recipient: str) -> None:
    flow = Flow()
    args = {"to": recipient, "body": "x"}
    decision = flow.judge(SEND, args)
    assert decision.outcome != "allow", recipient
    flow.add_step(decision, SEND, args)
    with pytest.raises(ExecutionRefusedError):
        flow.executor.execute(RUN, "s1", decision)
    assert flow.connector.calls == []


def test_conflicting_roles_cannot_approve_what_the_engine_could_not_decide() -> None:
    flow = Flow()
    decision = flow.judge("crm.mark_approved_for_signature", {"contract_id": "c1", "deal_value": 5})
    assert decision.outcome == "needs_approval" and decision.approver_role == "legal_reviewer"
    flow.add_step(
        decision, "crm.mark_approved_for_signature", {"contract_id": "c1", "deal_value": 5}
    )
    record = flow.approvals.request(run_id=RUN, decision=decision, requested_by=ALICE.user_id)
    with pytest.raises(ApproverNotAuthorizedError):
        flow.approvals.decide(
            record.approval_id,
            decider=Principal("fin", frozenset({"finance_approver"})),
            decision="approved",
            action_hash=decision.action_hash,
        )


def test_crash_and_resume_never_duplicates_the_external_effect() -> None:
    from tests.flow import Crash

    flow = Flow(lease=timedelta(minutes=1))
    decision = external(flow)
    flow.approve(decision)
    flow.connector.script = [Crash()]
    with pytest.raises(Crash):
        flow.executor.execute(RUN, "s1", decision)
    flow.clock.advance(
        hours=3
    )  # approval TTL has also lapsed: resume is exempt, new starts are not
    outcome = flow.executor.execute(RUN, "s1", decision)
    assert outcome.status is StepStatus.SUCCEEDED
    assert len(flow.connector.effects) == 1


def test_audit_chain_is_intact_after_attacks() -> None:
    from gawe.core.audit import verify_chain

    flow = Flow()
    decision = external(flow)
    with pytest.raises(ExecutionRefusedError):
        flow.executor.execute(RUN, "s1", decision)
    flow.approve(decision)
    flow.executor.execute(RUN, "s1", decision)
    assert verify_chain(flow.audit_store.events(RUN)).ok
