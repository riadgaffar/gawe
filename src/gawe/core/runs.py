"""Run records and their store.

A run holds what the orchestrator needs to continue after a restart: who is acting, the
objective, the validated plan, the policy decisions already made (the executor needs the exact
signed decision after a human approves), and findings from the Reader. The raw document is held
only until it has been read, then dropped.

Status changes are compare-and-set and follow the run state machine.
"""

import threading
from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any, Protocol

from pydantic import Field

from gawe.core.models import FrozenModel, Plan, PolicyDecision
from gawe.core.statemachine import RunStatus, check_run_transition

_UPDATABLE = frozenset(
    {
        "plan",
        "plan_version",
        "planner_model",
        "findings_schema_id",
        "findings",
        "document",
        "decisions",
        "error",
        "completed_at",
    }
)


def check_update_fields(updates: Mapping[str, Any]) -> None:
    """Only these fields may change through a store; status changes only via `transition`."""
    unknown = set(updates) - _UPDATABLE
    if unknown:
        raise ValueError(f"cannot update fields: {sorted(unknown)}")


class RunNotFoundError(KeyError):
    pass


class StaleRunError(Exception):
    """The run's status is not what the caller expected."""


class RunRecord(FrozenModel):
    run_id: str
    tenant_id: str
    acting_user_id: str
    roles: tuple[str, ...] = ()
    objective: str
    profile: str
    status: RunStatus = RunStatus.CREATED
    document: str | None = None
    findings_schema_id: str | None = None
    findings: dict[str, Any] | None = None
    plan: Plan | None = None
    plan_version: int = 0
    planner_model: str | None = None
    decisions: dict[str, PolicyDecision] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class RunStore(Protocol):
    def add(self, run: RunRecord) -> None: ...

    def get(self, run_id: str) -> RunRecord: ...

    def list_for_tenant(self, tenant_id: str, *, limit: int = 50) -> list[RunRecord]:
        """The tenant's runs, newest first."""
        ...

    def list_by_status(
        self, statuses: Collection[RunStatus], *, limit: int = 200
    ) -> list[RunRecord]:
        """Runs in any of the given statuses across all tenants, oldest first (for recovery)."""
        ...

    def transition(
        self, run_id: str, *, expected: RunStatus, to: RunStatus, **updates: Any
    ) -> RunRecord:
        """Atomically change status (and optionally other fields) if the status is `expected`."""
        ...

    def update(self, run_id: str, **updates: Any) -> RunRecord:
        """Change non-status fields."""
        ...


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    def add(self, run: RunRecord) -> None:
        with self._lock:
            if run.run_id in self._runs:
                raise ValueError(f"run {run.run_id} already exists")
            self._runs[run.run_id] = run

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            try:
                return self._runs[run_id]
            except KeyError:
                raise RunNotFoundError(f"no run {run_id}") from None

    def list_for_tenant(self, tenant_id: str, *, limit: int = 50) -> list[RunRecord]:
        with self._lock:
            mine = [r for r in self._runs.values() if r.tenant_id == tenant_id]
        return sorted(mine, key=lambda r: r.created_at, reverse=True)[:limit]

    def list_by_status(
        self, statuses: Collection[RunStatus], *, limit: int = 200
    ) -> list[RunRecord]:
        wanted = set(statuses)
        with self._lock:
            found = [r for r in self._runs.values() if r.status in wanted]
        return sorted(found, key=lambda r: r.created_at)[:limit]

    def transition(
        self, run_id: str, *, expected: RunStatus, to: RunStatus, **updates: Any
    ) -> RunRecord:
        check_run_transition(expected, to)
        check_update_fields(updates)
        return self._change(run_id, expected, {**updates, "status": to})

    def update(self, run_id: str, **updates: Any) -> RunRecord:
        """Change non-status fields. Status only ever changes through `transition`."""
        check_update_fields(updates)
        return self._change(run_id, None, updates)

    def _change(
        self, run_id: str, expected: RunStatus | None, updates: dict[str, Any]
    ) -> RunRecord:
        with self._lock:
            try:
                current = self._runs[run_id]
            except KeyError:
                raise RunNotFoundError(f"no run {run_id}") from None
            if expected is not None and current.status is not expected:
                raise StaleRunError(
                    f"run {run_id} is {current.status.value}, expected {expected.value}"
                )
            changed = RunRecord(**{**current.model_dump(), **updates})
            if "plan" in updates and (
                changed.plan is None or changed.plan_version <= current.plan_version
            ):
                raise ValueError("a new plan needs a plan_version higher than the current one")
            self._runs[run_id] = changed
            return changed
