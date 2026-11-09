"""Human approvals, bound to the exact action.

Rules enforced here (and, for the last two, again by database constraints):

* An approval is requested only from a validly signed `needs_approval` policy decision, and
  carries that decision's `action_hash` and approver role.
* The approver must echo the `action_hash` they were shown; a mismatch means they approved
  something other than what will run, so the decision is refused.
* The requester can never decide their own request (separation of duties).
* The approver must hold the role the decision names.
* Approvals expire. An approval cannot be decided after `expires_at`, and an approved action
  must start executing before it too. Resuming an already-started execution is exempt.
* At most one open approval per step, and each approval is decided at most once.

Refused attempts are written to the audit log (who tried, which rule stopped them), because a
refused self-approval is exactly the sort of event an audit reader wants to see.
"""

import hmac
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import model_validator

from gawe.core.audit import AuditLog
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.models import FrozenModel, PolicyDecision
from gawe.core.policy.signing import PolicySigner

DEFAULT_TTL = timedelta(hours=24)


class ApprovalError(Exception):
    """Base class for approval failures."""


class ApprovalNotFoundError(ApprovalError):
    pass


class ApprovalAlreadyOpenError(ApprovalError):
    pass


class ApprovalAlreadyDecidedError(ApprovalError):
    pass


class ApprovalExpiredError(ApprovalError):
    pass


class SeparationOfDutiesError(ApprovalError):
    pass


class ApproverNotAuthorizedError(ApprovalError):
    pass


class ActionHashMismatchError(ApprovalError):
    pass


class ApprovalNotGrantedError(ApprovalError):
    """There is no usable approval for this exact action."""


ApprovalState = Literal["none", "pending", "expired", "approved", "rejected"]


@dataclass(frozen=True)
class Principal:
    user_id: str
    roles: frozenset[str]


Decision = Literal["approved", "rejected"]


class ApprovalRecord(FrozenModel):
    approval_id: str
    run_id: str
    step_id: str
    action_hash: str
    approver_role: str
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    decided_by: str | None = None
    decision: Decision | None = None
    comment: str | None = None
    decided_at: datetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "ApprovalRecord":
        if self.expires_at <= self.requested_at:
            raise ValueError("expires_at must be after requested_at")
        decided = (self.decision, self.decided_by, self.decided_at)
        if any(x is None for x in decided) and not all(x is None for x in decided):
            raise ValueError("decision, decided_by and decided_at must be set together")
        if self.decided_by is not None and self.decided_by == self.requested_by:
            raise ValueError("the requester cannot decide their own approval")
        return self

    @property
    def is_open(self) -> bool:
        return self.decision is None


class ApprovalStore(Protocol):
    def add(self, record: ApprovalRecord) -> None:
        """Raises ApprovalAlreadyOpenError if the step already has an open approval."""
        ...

    def get(self, approval_id: str) -> ApprovalRecord: ...

    def for_step(self, run_id: str, step_id: str) -> list[ApprovalRecord]: ...

    def open_approvals(self) -> list[ApprovalRecord]: ...

    def record_decision(
        self,
        approval_id: str,
        *,
        decided_by: str,
        decision: Decision,
        comment: str | None,
        decided_at: datetime,
    ) -> ApprovalRecord:
        """Decide an open approval exactly once (ApprovalAlreadyDecidedError otherwise)."""
        ...


class InMemoryApprovalStore:
    """Atomic, like the database: one open approval per step, each decided once."""

    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}
        self._lock = threading.Lock()

    def add(self, record: ApprovalRecord) -> None:
        with self._lock:
            for existing in self._records.values():
                same_step = (existing.run_id, existing.step_id) == (record.run_id, record.step_id)
                if same_step and existing.is_open:
                    raise ApprovalAlreadyOpenError(
                        f"step {record.step_id} already has an open approval"
                    )
            self._records[record.approval_id] = record

    def get(self, approval_id: str) -> ApprovalRecord:
        with self._lock:
            try:
                return self._records[approval_id]
            except KeyError:
                raise ApprovalNotFoundError(f"no approval {approval_id}") from None

    def for_step(self, run_id: str, step_id: str) -> list[ApprovalRecord]:
        with self._lock:
            return [r for r in self._records.values() if (r.run_id, r.step_id) == (run_id, step_id)]

    def open_approvals(self) -> list[ApprovalRecord]:
        with self._lock:
            return [r for r in self._records.values() if r.is_open]

    def record_decision(
        self,
        approval_id: str,
        *,
        decided_by: str,
        decision: Decision,
        comment: str | None,
        decided_at: datetime,
    ) -> ApprovalRecord:
        with self._lock:
            try:
                current = self._records[approval_id]
            except KeyError:
                raise ApprovalNotFoundError(f"no approval {approval_id}") from None
            if not current.is_open:
                raise ApprovalAlreadyDecidedError(f"approval {approval_id} was already decided")
            decided = ApprovalRecord(
                **{
                    **current.model_dump(),
                    "decided_by": decided_by,
                    "decision": decision,
                    "comment": comment,
                    "decided_at": decided_at,
                }
            )
            self._records[approval_id] = decided
            return decided


SYSTEM_ACTOR = "gawe.system"


@dataclass(frozen=True)
class ApprovalStatus:
    state: ApprovalState
    record: ApprovalRecord | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class ApprovalService:
    def __init__(
        self,
        store: ApprovalStore,
        signer: PolicySigner,
        audit: AuditLog,
        *,
        clock: Callable[[], datetime] = _utc_now,
        default_ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        if default_ttl <= timedelta(0):
            raise ValueError("default_ttl must be positive")
        self._store = store
        self._signer = signer
        self._audit = audit
        self._clock = clock
        self._default_ttl = default_ttl

    def request(
        self,
        *,
        run_id: str,
        decision: PolicyDecision,
        requested_by: str,
        ttl: timedelta | None = None,
    ) -> ApprovalRecord:
        """Open an approval for a signed needs_approval decision."""
        if not self._signer.verify(decision):
            raise ApprovalError("policy decision signature is invalid")
        if decision.outcome != "needs_approval" or not decision.approver_role:
            raise ApprovalError("this policy decision does not call for an approval")
        lifetime = self._default_ttl if ttl is None else ttl
        if lifetime <= timedelta(0):
            raise ValueError("ttl must be positive")

        now = self._clock()
        record = ApprovalRecord(
            approval_id=str(uuid4()),
            run_id=run_id,
            step_id=decision.step_id,
            action_hash=decision.action_hash,
            approver_role=decision.approver_role,
            requested_by=requested_by,
            requested_at=now,
            expires_at=now + lifetime,
        )
        self._store.add(record)
        self._audit.record(
            run_id,
            "approval.requested",
            requested_by,
            {
                "approval_id": record.approval_id,
                "step_id": record.step_id,
                "action_hash": record.action_hash,
                "approver_role": record.approver_role,
                "expires_at": record.expires_at.isoformat(),
            },
        )
        return record

    def decide(
        self,
        approval_id: str,
        *,
        decider: Principal,
        decision: Decision,
        action_hash: str,
        comment: str | None = None,
    ) -> ApprovalRecord:
        record = self._store.get(approval_id)
        now = self._clock()

        refusal: ApprovalError | None = None
        reason = ""
        if not _same(action_hash, record.action_hash):
            reason, refusal = (
                "action_hash_mismatch",
                ActionHashMismatchError(
                    "the approver was shown a different action than the one awaiting approval"
                ),
            )
        elif not record.is_open:
            reason, refusal = (
                "already_decided",
                ApprovalAlreadyDecidedError(f"approval {approval_id} was already decided"),
            )
        elif now >= record.expires_at:
            reason, refusal = "expired", ApprovalExpiredError(f"approval {approval_id} has expired")
        elif decider.user_id == record.requested_by:
            reason, refusal = (
                "self_approval",
                SeparationOfDutiesError("the requester cannot decide their own approval"),
            )
        elif record.approver_role not in decider.roles:
            reason, refusal = (
                "missing_role",
                ApproverNotAuthorizedError(
                    f"approving this action requires the {record.approver_role} role"
                ),
            )

        if refusal is not None:
            self._audit.record(
                record.run_id,
                "approval.refused",
                decider.user_id,
                {"approval_id": approval_id, "step_id": record.step_id, "reason": reason},
            )
            raise refusal

        decided = self._store.record_decision(
            approval_id,
            decided_by=decider.user_id,
            decision=decision,
            comment=comment,
            decided_at=now,
        )
        self._audit.record(
            record.run_id,
            "approval.decided",
            decider.user_id,
            {
                "approval_id": approval_id,
                "step_id": record.step_id,
                "action_hash": record.action_hash,
                "decision": decision,
                # The comment may be sensitive; the log keeps only its hash.
                "comment_sha256": None if comment is None else sha256_hex(canonical_json(comment)),
            },
        )
        return decided

    def require_approved(
        self, *, run_id: str, step_id: str, action_hash: str, enforce_expiry: bool = True
    ) -> ApprovalRecord:
        """Return the approval authorizing exactly this action, or raise ApprovalNotGrantedError.

        `enforce_expiry=False` is for resuming an execution that already started under a valid
        approval; it must not be used to begin a new one.
        """
        now = self._clock()
        matching = [
            r for r in self._store.for_step(run_id, step_id) if _same(r.action_hash, action_hash)
        ]
        if not matching:
            raise ApprovalNotGrantedError("no approval exists for this exact action")
        approved = [r for r in matching if r.decision == "approved"]
        if not approved:
            state = "rejected" if any(r.decision == "rejected" for r in matching) else "pending"
            raise ApprovalNotGrantedError(f"the approval for this action is {state}")
        for record in approved:
            if not enforce_expiry or now < record.expires_at:
                return record
        raise ApprovalNotGrantedError("the approval for this action has expired")

    def state_for(self, *, run_id: str, step_id: str, action_hash: str) -> ApprovalStatus:
        """Where the approval for exactly this action stands (latest request for that hash)."""
        now = self._clock()
        matching = [
            r for r in self._store.for_step(run_id, step_id) if _same(r.action_hash, action_hash)
        ]
        if not matching:
            return ApprovalStatus("none")
        latest = max(matching, key=lambda r: r.requested_at)
        if latest.decision == "approved":
            return ApprovalStatus("approved", latest)
        if latest.decision == "rejected":
            return ApprovalStatus("rejected", latest)
        return ApprovalStatus("expired" if now >= latest.expires_at else "pending", latest)

    def expired_open(self) -> list[ApprovalRecord]:
        """Open approvals past their deadline; the orchestrator fails the waiting steps."""
        now = self._clock()
        return [r for r in self._store.open_approvals() if now >= r.expires_at]

    def close_for_run(self, run_id: str, *, reason: str) -> int:
        """Reject every open approval of a run that has ended, so none can be approved later.

        The decision is recorded as a rejection by the system, with the reason as its comment, and
        an `approval.closed` audit event. Returns how many were closed. A concurrent human
        decision wins: an approval that was just decided is skipped, not an error.
        """
        closed = 0
        for record in self._store.open_approvals():
            if record.run_id != run_id:
                continue
            try:
                self._store.record_decision(
                    record.approval_id,
                    decided_by=SYSTEM_ACTOR,
                    decision="rejected",
                    comment=reason,
                    decided_at=self._clock(),
                )
            except ApprovalAlreadyDecidedError:
                continue
            self._audit.record(
                run_id,
                "approval.closed",
                SYSTEM_ACTOR,
                {"approval_id": record.approval_id, "step_id": record.step_id, "reason": reason},
            )
            closed += 1
        return closed
