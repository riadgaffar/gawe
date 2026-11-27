"""Durable, tenant-scoped jobs. CAS revisions fence workers; deadlines never move.

Job creation precedes dispatch. A persisted result precedes step/run continuation, so recovery
can replay continuation after any crash without performing the external write again.
"""

import threading
from datetime import datetime
from typing import Any, NamedTuple, Protocol

from pydantic import AwareDatetime, Field, model_validator

from gawe.core.models import FrozenModel


class JobConflictError(Exception):
    """A revision, lease or immutable identity no longer matches."""


# Kinds that belong to a tenant rather than a run: their run fields are all null.
TENANT_KINDS = frozenset({"daily_review"})


class MaintenanceJob(FrozenModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str = Field(min_length=1, max_length=128)
    tenant_id: str
    run_id: str | None
    plan_version: int | None = Field(ge=1)
    step_id: str | None
    action_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    operation_key: str = Field(min_length=1, max_length=256)
    created_at: AwareDatetime
    deadline: AwareDatetime
    due_at: AwareDatetime
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    evidence_seq: int | None = Field(default=None, ge=1)
    protocol_error: str | None = None
    result: dict[str, Any] | None = None
    revision: int = Field(default=0, ge=0)
    lease_owner: str | None = None
    lease_until: AwareDatetime | None = None
    continued: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "MaintenanceJob":
        run_fields = (self.run_id, self.plan_version, self.step_id, self.action_hash)
        if self.kind in TENANT_KINDS:
            if any(f is not None for f in run_fields):
                raise ValueError(f"a {self.kind} job belongs to a tenant, not a run")
        elif any(f is None for f in run_fields):
            raise ValueError(f"a {self.kind} job needs its run, plan version, step and action")
        if self.deadline < self.created_at:
            raise ValueError("deadline precedes intent")
        if (self.lease_owner is None) != (self.lease_until is None):
            raise ValueError("lease requires both owner and expiry")
        if self.continued and self.result is None:
            raise ValueError("unresolved job cannot acknowledge continuation")
        return self


class RunTarget(NamedTuple):
    run_id: str
    plan_version: int
    step_id: str
    action_hash: str


def run_target(job: MaintenanceJob) -> RunTarget:
    """The run, plan, step and action of a run-scoped job (every kind but tenant kinds)."""
    if (
        job.run_id is None
        or job.plan_version is None
        or job.step_id is None
        or job.action_hash is None
    ):
        raise ValueError(f"{job.kind} job {job.job_id} is not run-scoped")
    return RunTarget(job.run_id, job.plan_version, job.step_id, job.action_hash)


IMMUTABLE = frozenset(
    {
        "job_id",
        "kind",
        "tenant_id",
        "run_id",
        "plan_version",
        "step_id",
        "action_hash",
        "operation_key",
        "created_at",
        "deadline",
        "payload",
    }
)


def validate_job_change(current: MaintenanceJob, changed: MaintenanceJob) -> None:
    MaintenanceJob.model_validate(changed.model_dump())
    if any(getattr(current, key) != getattr(changed, key) for key in IMMUTABLE):
        raise JobConflictError("immutable job identity or intent changed")
    if changed.revision != current.revision + 1:
        raise JobConflictError("job revision changed")
    proof = current.evidence.get("refusal")
    if proof is not None:
        if changed.evidence.get("refusal") != proof:
            raise JobConflictError("refusal proof cannot change")
        if changed.result is not None and changed.result != {
            "outcome": "not_dispatched",
            "code": proof["code"],
        }:
            raise JobConflictError("a refusal cannot become another outcome")
    if current.result is not None and changed.result != current.result:
        raise JobConflictError("terminal job result cannot change")
    if current.result is not None and (
        changed.evidence != current.evidence or changed.protocol_error != current.protocol_error
    ):
        raise JobConflictError("terminal evidence cannot change")
    if current.continued and not changed.continued:
        raise JobConflictError("continuation cannot be undone")
    if current.protocol_error is not None and changed.protocol_error != current.protocol_error:
        raise JobConflictError("protocol failure cannot be cleared")


class MaintenanceStore(Protocol):
    def add(self, job: MaintenanceJob) -> bool:
        """Insert once, returning whether inserted. A different intent at this id conflicts."""
        ...

    def get(self, tenant_id: str, job_id: str) -> MaintenanceJob | None: ...

    def for_run(self, tenant_id: str, run_id: str) -> list[MaintenanceJob]: ...

    def due(
        self,
        now: datetime,
        *,
        limit: int = 200,
        after: tuple[datetime, str] | None = None,
    ) -> list[MaintenanceJob]:
        """Unleased, unacknowledged jobs due by `now`, ordered by (due_at, job_id).

        `after` is an exclusive (due_at, job_id) cursor, so one pass can page past a job that
        failed without retrying it in the same pass.
        """
        ...

    def counts(self, kind: str) -> dict[str, int]:
        """Jobs of one kind by result outcome, `unresolved` for none. Includes acknowledged
        jobs, so a terminal outcome stays visible after it leaves the due queue."""
        ...

    def replace(self, job: MaintenanceJob, *, expected_revision: int) -> MaintenanceJob: ...


class InMemoryMaintenanceStore:
    def __init__(self) -> None:
        self._jobs: dict[str, MaintenanceJob] = {}
        self._lock = threading.Lock()

    def add(self, job: MaintenanceJob) -> bool:
        with self._lock:
            old = self._jobs.get(job.job_id)
            if old is not None:
                if any(getattr(old, k) != getattr(job, k) for k in IMMUTABLE):
                    raise JobConflictError("conflicting job intent")
                return False
            self._jobs[job.job_id] = job.model_copy(deep=True)
            return True

    def get(self, tenant_id: str, job_id: str) -> MaintenanceJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.model_copy(deep=True) if job and job.tenant_id == tenant_id else None

    def for_run(self, tenant_id: str, run_id: str) -> list[MaintenanceJob]:
        with self._lock:
            return [
                j.model_copy(deep=True)
                for j in self._jobs.values()
                if j.tenant_id == tenant_id and j.run_id == run_id
            ]

    def due(
        self,
        now: datetime,
        *,
        limit: int = 200,
        after: tuple[datetime, str] | None = None,
    ) -> list[MaintenanceJob]:
        with self._lock:
            jobs = [
                j
                for j in self._jobs.values()
                if not j.continued
                and j.due_at <= now
                and (j.lease_until is None or j.lease_until <= now)
                and (after is None or (j.due_at, j.job_id) > after)
            ]
            return [
                j.model_copy(deep=True)
                for j in sorted(jobs, key=lambda j: (j.due_at, j.job_id))[: max(0, limit)]
            ]

    def counts(self, kind: str) -> dict[str, int]:
        with self._lock:
            totals: dict[str, int] = {}
            for job in self._jobs.values():
                if job.kind == kind:
                    outcome = str(job.result["outcome"]) if job.result else "unresolved"
                    totals[outcome] = totals.get(outcome, 0) + 1
            return totals

    def replace(self, job: MaintenanceJob, *, expected_revision: int) -> MaintenanceJob:
        with self._lock:
            old = self._jobs.get(job.job_id)
            if old is None or old.revision != expected_revision:
                raise JobConflictError("stale job revision")
            validate_job_change(old, job)
            self._jobs[job.job_id] = job.model_copy(deep=True)
            return job
