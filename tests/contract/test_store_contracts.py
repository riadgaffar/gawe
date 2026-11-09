"""Behavior every StepStore / ApprovalStore / ExecutionStore implementation must share."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from gawe.core.approvals import (
    ApprovalAlreadyDecidedError,
    ApprovalAlreadyOpenError,
    ApprovalNotFoundError,
    ApprovalRecord,
)
from gawe.core.records import (
    AttemptConflictError,
    StaleStateError,
    StepNotFoundError,
    StepRecord,
)
from gawe.core.statemachine import IllegalTransitionError, StepStatus
from tests.contract.conftest import ALICE, BOB, Backend

H = "a" * 64
H2 = "b" * 64
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def add_step(b: Backend, step_id: str = "s1", **kw: object) -> StepRecord:
    fields: dict[str, object] = {
        "run_id": b.run_id,
        "step_id": step_id,
        "tool": "contracts.send_email",
        "tool_version": "1.0.0",
        "status": StepStatus.READY,
    }
    record = StepRecord(**{**fields, **kw})  # type: ignore[arg-type]
    b.steps.add(record)
    return record


def approval(b: Backend, step_id: str = "s1", **kw: object) -> ApprovalRecord:
    fields: dict[str, object] = {
        "approval_id": str(uuid.uuid4()),
        "run_id": b.run_id,
        "step_id": step_id,
        "action_hash": H,
        "approver_role": "legal_reviewer",
        "requested_by": ALICE,
        "requested_at": T0,
        "expires_at": T0 + timedelta(hours=1),
    }
    return ApprovalRecord(**{**fields, **kw})  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- steps


def test_step_round_trips_every_field(backend: Backend) -> None:
    record = add_step(
        backend,
        args_template={"to": {"$ref": "s0.output.address"}, "body": "hi"},
        depends_on=("s0",),
        resolved_args={"to": "a@b.com", "body": "hi"},
    )
    assert backend.steps.get(backend.run_id, "s1") == record


def test_duplicate_step_is_rejected(backend: Backend) -> None:
    add_step(backend)
    with pytest.raises(ValueError, match="already exists"):
        add_step(backend)


def test_unknown_step(backend: Backend) -> None:
    with pytest.raises(StepNotFoundError):
        backend.steps.get(backend.run_id, "nope")
    with pytest.raises(StepNotFoundError):
        backend.steps.transition(
            backend.run_id, "nope", expected=StepStatus.READY, to=StepStatus.POLICY_CHECKED
        )


def test_transition_persists_changes(backend: Backend) -> None:
    add_step(backend, resolved_args={"to": "a@b.com"})
    changed = backend.steps.transition(
        backend.run_id,
        "s1",
        expected=StepStatus.READY,
        to=StepStatus.POLICY_CHECKED,
        action_hash=H,
    )
    stored = backend.steps.get(backend.run_id, "s1")
    assert stored == changed
    assert (stored.status, stored.action_hash) == (StepStatus.POLICY_CHECKED, H)


def test_transition_is_compare_and_set(backend: Backend) -> None:
    add_step(backend)
    backend.steps.transition(
        backend.run_id, "s1", expected=StepStatus.READY, to=StepStatus.POLICY_CHECKED, action_hash=H
    )
    with pytest.raises(StaleStateError):
        backend.steps.transition(
            backend.run_id, "s1", expected=StepStatus.READY, to=StepStatus.FAILED
        )
    assert backend.steps.get(backend.run_id, "s1").status is StepStatus.POLICY_CHECKED


def test_transition_rejects_illegal_unknown_and_hashless(backend: Backend) -> None:
    add_step(backend)
    with pytest.raises(IllegalTransitionError):
        backend.steps.transition(
            backend.run_id, "s1", expected=StepStatus.READY, to=StepStatus.EXECUTING
        )
    with pytest.raises(ValueError, match="cannot update"):
        backend.steps.transition(
            backend.run_id, "s1", expected=StepStatus.READY, to=StepStatus.FAILED, tool="other"
        )
    with pytest.raises(ValidationError):  # past policy requires the action hash
        backend.steps.transition(
            backend.run_id, "s1", expected=StepStatus.READY, to=StepStatus.POLICY_CHECKED
        )
    assert backend.steps.get(backend.run_id, "s1").status is StepStatus.READY


def test_failure_records_a_short_error(backend: Backend) -> None:
    add_step(backend)
    backend.steps.transition(
        backend.run_id, "s1", expected=StepStatus.READY, to=StepStatus.FAILED, error="boom"
    )
    stored = backend.steps.get(backend.run_id, "s1")
    assert (stored.status, stored.error) == (StepStatus.FAILED, "boom")


# -------------------------------------------------------------------------- approvals


def test_approval_round_trips_and_is_listed(backend: Backend) -> None:
    add_step(backend)
    record = approval(backend)
    backend.approvals.add(record)
    assert backend.approvals.get(record.approval_id) == record
    assert backend.approvals.for_step(backend.run_id, "s1") == [record]
    assert record in backend.approvals.open_approvals()
    assert backend.approvals.for_step(backend.run_id, "other") == []


def test_only_one_open_approval_per_step(backend: Backend) -> None:
    add_step(backend)
    backend.approvals.add(approval(backend))
    with pytest.raises(ApprovalAlreadyOpenError):
        backend.approvals.add(approval(backend))


def test_decision_is_recorded_once(backend: Backend) -> None:
    add_step(backend)
    record = approval(backend)
    backend.approvals.add(record)
    decided = backend.approvals.record_decision(
        record.approval_id,
        decided_by=BOB,
        decision="approved",
        comment="ok",
        decided_at=T0 + timedelta(minutes=5),
    )
    assert backend.approvals.get(record.approval_id) == decided
    assert (decided.decision, decided.decided_by, decided.comment) == ("approved", BOB, "ok")
    assert record.approval_id not in [r.approval_id for r in backend.approvals.open_approvals()]
    with pytest.raises(ApprovalAlreadyDecidedError):
        backend.approvals.record_decision(
            record.approval_id, decided_by=BOB, decision="rejected", comment=None, decided_at=T0
        )


def test_a_decided_approval_frees_the_step_for_a_new_request(backend: Backend) -> None:
    add_step(backend)
    first = approval(backend)
    backend.approvals.add(first)
    backend.approvals.record_decision(
        first.approval_id,
        decided_by=BOB,
        decision="rejected",
        comment=None,
        decided_at=T0 + timedelta(minutes=1),
    )
    backend.approvals.add(approval(backend))
    assert len(backend.approvals.for_step(backend.run_id, "s1")) == 2


def test_unknown_approval_ids(backend: Backend) -> None:
    for bad in ("nope", str(uuid.uuid4())):
        with pytest.raises(ApprovalNotFoundError):
            backend.approvals.get(bad)
        with pytest.raises(ApprovalNotFoundError):
            backend.approvals.record_decision(
                bad, decided_by=BOB, decision="approved", comment=None, decided_at=T0
            )


def test_requester_cannot_decide_their_own_approval(backend: Backend) -> None:
    add_step(backend)
    record = approval(backend)
    backend.approvals.add(record)
    with pytest.raises(ValidationError):
        backend.approvals.record_decision(
            record.approval_id, decided_by=ALICE, decision="approved", comment=None, decided_at=T0
        )
    assert backend.approvals.get(record.approval_id).is_open


# -------------------------------------------------------------------------- executions


def test_attempts_are_numbered_in_sequence(backend: Backend) -> None:
    add_step(backend)
    b = backend
    b.executions.start(b.run_id, "s1", attempt=1, idempotency_key="k", now=T0)
    for bad in (1, 3, 0):
        with pytest.raises(AttemptConflictError):
            b.executions.start(b.run_id, "s1", attempt=bad, idempotency_key="k", now=T0)
    b.executions.finish(b.run_id, "s1", attempt=1, status="failed", now=T0, error="boom")
    b.executions.start(b.run_id, "s1", attempt=2, idempotency_key="k", now=T0)
    history = b.executions.attempts(b.run_id, "s1")
    assert [(a.attempt, a.status) for a in history] == [(1, "failed"), (2, "started")]
    assert history[0].error == "boom" and history[0].finished_at == T0


def test_an_attempt_finishes_exactly_once(backend: Backend) -> None:
    add_step(backend)
    b = backend
    b.executions.start(b.run_id, "s1", attempt=1, idempotency_key="k", now=T0)
    done = b.executions.finish(b.run_id, "s1", attempt=1, status="succeeded", now=T0, output_hash=H)
    assert b.executions.attempts(b.run_id, "s1") == [done]
    assert (done.idempotency_key, done.output_hash) == ("k", H)
    with pytest.raises(AttemptConflictError):
        b.executions.finish(b.run_id, "s1", attempt=1, status="failed", now=T0)
    with pytest.raises(AttemptConflictError):
        b.executions.finish(b.run_id, "s1", attempt=7, status="failed", now=T0)


def test_output_is_write_once_and_round_trips(backend: Backend) -> None:
    add_step(backend)
    b = backend
    assert b.executions.load_output(b.run_id, "s1") is None
    first = {"message_id": "m1", "nested": {"n": [1, 2, {"x": None}]}, "ok": True}
    b.executions.save_output(b.run_id, "s1", first)
    b.executions.save_output(b.run_id, "s1", {"message_id": "OTHER"})
    assert b.executions.load_output(b.run_id, "s1") == first


def test_attempts_for_a_step_without_history_is_empty(backend: Backend) -> None:
    add_step(backend)
    assert backend.executions.attempts(backend.run_id, "s1") == []
