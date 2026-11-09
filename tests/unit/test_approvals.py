from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tests.helpers import signer

from gawe.core.approvals import (
    SYSTEM_ACTOR,
    ActionHashMismatchError,
    ApprovalAlreadyDecidedError,
    ApprovalAlreadyOpenError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalNotGrantedError,
    ApprovalRecord,
    ApprovalService,
    ApproverNotAuthorizedError,
    InMemoryApprovalStore,
    Principal,
    SeparationOfDutiesError,
)
from gawe.core.audit import AuditLog, InMemoryAuditStore, verify_chain

H = "a" * 64
OTHER = "b" * 64
RUN = "run-1"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


ALICE = Principal("alice", frozenset({"case_manager"}))
BOB = Principal("bob", frozenset({"legal_reviewer"}))


class Env:
    def __init__(self) -> None:
        self.clock = Clock()
        self.audit_store = InMemoryAuditStore()
        self.audit = AuditLog(self.audit_store)
        self.store = InMemoryApprovalStore()
        self.service = ApprovalService(
            self.store, signer(), self.audit, clock=self.clock, default_ttl=timedelta(hours=1)
        )

    def decision(self, *, step: str = "s1", action: str = H, role: str | None = "legal_reviewer"):  # type: ignore[no-untyped-def]
        return signer().seal(
            step_id=step,
            action_hash=action,
            outcome="needs_approval",
            rule_ids=["r"],
            reason="x",
            approver_role=role,
        )

    def request(self, **kw):  # type: ignore[no-untyped-def]
        return self.service.request(run_id=RUN, decision=self.decision(**kw), requested_by="alice")

    def audit_types(self) -> list[str]:
        return [e.type for e in self.audit_store.events(RUN)]


@pytest.fixture
def env() -> Env:
    return Env()


def test_request_binds_hash_role_and_expiry(env: Env) -> None:
    rec = env.request()
    assert (rec.action_hash, rec.approver_role) == (H, "legal_reviewer")
    assert rec.expires_at == T0 + timedelta(hours=1)
    assert rec.is_open


def test_request_rejects_forged_or_wrong_kind_of_decision(env: Env) -> None:
    forged = env.decision().model_copy(update={"action_hash": OTHER})
    with pytest.raises(ApprovalError, match="signature"):
        env.service.request(run_id=RUN, decision=forged, requested_by="alice")
    allow = signer().seal(step_id="s1", action_hash=H, outcome="allow", rule_ids=[], reason="x")
    with pytest.raises(ApprovalError, match="does not call"):
        env.service.request(run_id=RUN, decision=allow, requested_by="alice")


def test_only_one_open_approval_per_step(env: Env) -> None:
    env.request()
    with pytest.raises(ApprovalAlreadyOpenError):
        env.request()


def test_approve_happy_path_then_require_approved(env: Env) -> None:
    rec = env.request()
    done = env.service.decide(rec.approval_id, decider=BOB, decision="approved", action_hash=H)
    assert done.decision == "approved" and done.decided_by == "bob"
    env.service.require_approved(run_id=RUN, step_id="s1", action_hash=H)
    assert env.audit_types() == ["approval.requested", "approval.decided"]
    assert verify_chain(env.audit_store.events(RUN)).ok


def test_wrong_action_hash_is_refused_and_audited(env: Env) -> None:
    rec = env.request()
    with pytest.raises(ActionHashMismatchError):
        env.service.decide(rec.approval_id, decider=BOB, decision="approved", action_hash=OTHER)
    assert env.store.get(rec.approval_id).is_open
    assert env.audit_types()[-1] == "approval.refused"


def test_self_approval_is_refused(env: Env) -> None:
    rec = env.request()
    both = Principal("alice", frozenset({"legal_reviewer"}))
    with pytest.raises(SeparationOfDutiesError):
        env.service.decide(rec.approval_id, decider=both, decision="approved", action_hash=H)
    assert env.store.get(rec.approval_id).is_open


def test_approver_needs_the_named_role(env: Env) -> None:
    rec = env.request()
    with pytest.raises(ApproverNotAuthorizedError):
        env.service.decide(
            rec.approval_id,
            decider=Principal("carol", frozenset({"case_manager"})),
            decision="approved",
            action_hash=H,
        )


def test_expired_approval_cannot_be_decided(env: Env) -> None:
    rec = env.request()
    env.clock.advance(hours=1)  # boundary: expires_at itself is already too late
    with pytest.raises(ApprovalExpiredError):
        env.service.decide(rec.approval_id, decider=BOB, decision="approved", action_hash=H)
    assert [r.approval_id for r in env.service.expired_open()] == [rec.approval_id]


def test_decision_is_final(env: Env) -> None:
    rec = env.request()
    env.service.decide(rec.approval_id, decider=BOB, decision="rejected", action_hash=H)
    with pytest.raises(ApprovalAlreadyDecidedError):
        env.service.decide(rec.approval_id, decider=BOB, decision="approved", action_hash=H)
    with pytest.raises(ApprovalNotGrantedError, match="rejected"):
        env.service.require_approved(run_id=RUN, step_id="s1", action_hash=H)


def test_unknown_approval(env: Env) -> None:
    with pytest.raises(ApprovalNotFoundError):
        env.service.decide("nope", decider=BOB, decision="approved", action_hash=H)


def test_require_approved_states(env: Env) -> None:
    with pytest.raises(ApprovalNotGrantedError, match="no approval"):
        env.service.require_approved(run_id=RUN, step_id="s1", action_hash=H)
    rec = env.request()
    with pytest.raises(ApprovalNotGrantedError, match="pending"):
        env.service.require_approved(run_id=RUN, step_id="s1", action_hash=H)
    env.service.decide(rec.approval_id, decider=BOB, decision="approved", action_hash=H)
    # an approval for hash H never authorizes a drifted action
    with pytest.raises(ApprovalNotGrantedError, match="no approval"):
        env.service.require_approved(run_id=RUN, step_id="s1", action_hash=OTHER)


def test_approval_expires_before_execution_but_resume_is_exempt(env: Env) -> None:
    rec = env.request()
    env.service.decide(rec.approval_id, decider=BOB, decision="approved", action_hash=H)
    env.clock.advance(hours=2)
    with pytest.raises(ApprovalNotGrantedError, match="expired"):
        env.service.require_approved(run_id=RUN, step_id="s1", action_hash=H)
    env.service.require_approved(run_id=RUN, step_id="s1", action_hash=H, enforce_expiry=False)


def test_comment_is_hashed_not_logged(env: Env) -> None:
    rec = env.request()
    env.service.decide(
        rec.approval_id, decider=BOB, decision="approved", action_hash=H, comment="secret text"
    )
    assert "secret text" not in repr(env.audit_store.events(RUN))


def test_record_invariants() -> None:
    base = {
        "approval_id": "a", "run_id": "r", "step_id": "s", "action_hash": H,
        "approver_role": "x", "requested_by": "alice", "requested_at": T0,
        "expires_at": T0 + timedelta(hours=1),
    }  # fmt: skip
    with pytest.raises(ValidationError):
        ApprovalRecord(**{**base, "expires_at": T0})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ApprovalRecord(**{**base, "decision": "approved"})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ApprovalRecord(
            **{**base, "decision": "approved", "decided_by": "alice", "decided_at": T0}  # type: ignore[arg-type]
        )


def test_default_ttl_must_be_positive(env: Env) -> None:
    with pytest.raises(ValueError):
        ApprovalService(env.store, signer(), env.audit, default_ttl=timedelta(0))


def test_state_for_tracks_the_latest_request_for_the_exact_action() -> None:
    from tests.flow import BOB, EXTERNAL_EMAIL, Flow

    f = Flow()
    decision = f.judge("contracts.send_email", EXTERNAL_EMAIL)
    kwargs = {"run_id": "run1", "step_id": decision.step_id, "action_hash": decision.action_hash}
    assert f.approvals.state_for(**kwargs).state == "none"
    record = f.approvals.request(run_id="run1", decision=decision, requested_by="alice")
    assert f.approvals.state_for(**kwargs).state == "pending"
    other = {**kwargs, "action_hash": "0" * 64}
    assert f.approvals.state_for(**other).state == "none"
    f.clock.advance(hours=2)
    assert f.approvals.state_for(**kwargs).state == "expired"
    f.clock.advance(hours=-2)
    f.approvals.decide(
        record.approval_id, decider=BOB, decision="rejected", action_hash=decision.action_hash
    )
    assert f.approvals.state_for(**kwargs).state == "rejected"


def test_close_for_run_rejects_open_approvals_as_the_system(env: Env) -> None:
    rec = env.request()
    assert env.service.close_for_run(RUN, reason="run_cancelled") == 1
    closed = env.store.get(rec.approval_id)
    assert (closed.decision, closed.decided_by, closed.comment) == (
        "rejected",
        SYSTEM_ACTOR,
        "run_cancelled",
    )
    assert env.audit_types()[-1] == "approval.closed"
    assert verify_chain(env.audit_store.events(RUN)).ok


def test_a_closed_approval_cannot_be_approved_later(env: Env) -> None:
    rec = env.request()
    env.service.close_for_run(RUN, reason="run_cancelled")
    with pytest.raises(ApprovalAlreadyDecidedError):
        env.service.decide(rec.approval_id, decider=BOB, decision="approved", action_hash=H)


def test_close_for_run_leaves_other_runs_and_decided_approvals_alone(env: Env) -> None:
    decided = env.request()
    env.service.decide(decided.approval_id, decider=BOB, decision="approved", action_hash=H)
    assert env.service.close_for_run(RUN, reason="x") == 0  # nothing open
    assert env.store.get(decided.approval_id).decision == "approved"
    assert env.service.close_for_run("another-run", reason="x") == 0
