"""Postgres implementation of RunStore.

A run row holds status and the run-level state the orchestrator resumes from. The validated plan
lives in `plans` (one row per version) and `runs.current_plan_version` says which is current;
steps are resolved through that version, so a new plan version never inherits old step state.

Every change locks the row (`FOR UPDATE`), compares the status with the caller's `expected`, checks
the result with the same Pydantic model the in-memory store uses, and repeats the status check in
the UPDATE. Exactly one concurrent caller wins; the rest get StaleRunError.
"""

import json
from collections.abc import Collection
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from gawe.core.models import Plan, PolicyDecision
from gawe.core.runs import RunNotFoundError, RunRecord, StaleRunError, check_update_fields
from gawe.core.statemachine import RunStatus, check_run_transition
from gawe.db.bind import Bind, connection_for

_UNIQUE_VIOLATION = "23505"

_COLUMNS = (
    "CAST(r.id AS text) AS run_id, CAST(r.tenant_id AS text) AS tenant_id, "
    "CAST(r.acting_user_id AS text) AS acting_user_id, r.roles, r.objective, r.profile, "
    "r.status, r.document, r.findings_schema_id, r.findings, r.decisions, r.error, "
    "r.created_at, r.completed_at, r.current_plan_version AS plan_version, "
    "(SELECT p.steps FROM plans p WHERE p.run_id = r.id AND p.version = r.current_plan_version) "
    "AS plan_steps, "
    "(SELECT p.planner_model FROM plans p "
    "WHERE p.run_id = r.id AND p.version = r.current_plan_version) AS planner_model"
)
_SELECT = text(f"SELECT {_COLUMNS} FROM runs r WHERE r.id = CAST(:run_id AS uuid)")
_SELECT_LOCKED = text(
    f"SELECT {_COLUMNS} FROM runs r WHERE r.id = CAST(:run_id AS uuid) FOR UPDATE OF r"
)
_LIST = text(
    f"SELECT {_COLUMNS} FROM runs r WHERE r.tenant_id = CAST(:tenant_id AS uuid) "
    "ORDER BY r.created_at DESC, r.id LIMIT :limit"
)
_LIST_BY_STATUS = text(
    f"SELECT {_COLUMNS} FROM runs r WHERE r.status = ANY(CAST(:statuses AS text[])) "
    "ORDER BY r.created_at, r.id LIMIT :limit"
)
_INSERT = text(
    "INSERT INTO runs (id, tenant_id, acting_user_id, roles, objective, profile, status, "
    "document, created_at) VALUES (CAST(:run_id AS uuid), CAST(:tenant_id AS uuid), "
    "CAST(:acting_user_id AS uuid), :roles, :objective, :profile, :status, :document, "
    ":created_at)"
)
_INSERT_PLAN = text(
    "INSERT INTO plans (run_id, version, steps, planner_model, validated_at) "
    "VALUES (CAST(:run_id AS uuid), :version, CAST(:steps AS jsonb), :model, now())"
)
_UPDATE = text(
    "UPDATE runs SET status = :to, document = :document, findings_schema_id = :schema_id, "
    "findings = CAST(:findings AS jsonb), decisions = CAST(:decisions AS jsonb), "
    "error = :error, completed_at = :completed_at, current_plan_version = :plan_version, "
    "updated_at = now() "
    "WHERE id = CAST(:run_id AS uuid) AND status = :current"
)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _to_record(row: Any) -> RunRecord:
    plan = Plan(steps=row["plan_steps"]) if row["plan_steps"] is not None else None
    return RunRecord(
        run_id=row["run_id"],
        tenant_id=row["tenant_id"],
        acting_user_id=row["acting_user_id"],
        roles=tuple(row["roles"] or ()),
        objective=row["objective"],
        profile=row["profile"],
        status=RunStatus(row["status"]),
        document=row["document"],
        findings_schema_id=row["findings_schema_id"],
        findings=row["findings"],
        plan=plan,
        plan_version=row["plan_version"],
        planner_model=row["planner_model"],
        decisions={k: PolicyDecision(**v) for k, v in (row["decisions"] or {}).items()},
        error=row["error"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


class PostgresRunStore:
    """Pass a Connection to join the caller's transaction, or an Engine to commit per call."""

    def __init__(self, bind: Bind) -> None:
        self._bind = bind

    def add(self, run: RunRecord) -> None:
        if run.plan is not None or run.plan_version != 0:
            raise ValueError("a run is added without a plan; the plan is saved by update()")
        params = {
            "run_id": run.run_id,
            "tenant_id": run.tenant_id,
            "acting_user_id": run.acting_user_id,
            "roles": list(run.roles),
            "objective": run.objective,
            "profile": run.profile,
            "status": run.status.value,
            "document": run.document,
            "created_at": run.created_at,
        }
        with connection_for(self._bind) as conn:
            try:
                with conn.begin_nested():
                    conn.execute(_INSERT, params)
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
                    raise ValueError(f"run {run.run_id} already exists") from exc
                raise

    def get(self, run_id: str) -> RunRecord:
        with connection_for(self._bind) as conn:
            row = conn.execute(_SELECT, {"run_id": run_id}).mappings().first()
        if row is None:
            raise RunNotFoundError(f"no run {run_id}")
        return _to_record(row)

    def list_for_tenant(self, tenant_id: str, *, limit: int = 50) -> list[RunRecord]:
        with connection_for(self._bind) as conn:
            rows = conn.execute(_LIST, {"tenant_id": tenant_id, "limit": limit}).mappings().all()
        return [_to_record(row) for row in rows]

    def list_by_status(
        self, statuses: Collection[RunStatus], *, limit: int = 200
    ) -> list[RunRecord]:
        names = sorted(status.value for status in statuses)
        if not names:
            return []
        with connection_for(self._bind) as conn:
            rows = (
                conn.execute(_LIST_BY_STATUS, {"statuses": names, "limit": limit}).mappings().all()
            )
        return [_to_record(row) for row in rows]

    def transition(
        self, run_id: str, *, expected: RunStatus, to: RunStatus, **updates: Any
    ) -> RunRecord:
        check_run_transition(expected, to)
        check_update_fields(updates)
        return self._change(run_id, expected, to, updates)

    def update(self, run_id: str, **updates: Any) -> RunRecord:
        check_update_fields(updates)
        return self._change(run_id, None, None, updates)

    def _change(
        self, run_id: str, expected: RunStatus | None, to: RunStatus | None, updates: dict[str, Any]
    ) -> RunRecord:
        with connection_for(self._bind) as conn:
            row = conn.execute(_SELECT_LOCKED, {"run_id": run_id}).mappings().first()
            if row is None:
                raise RunNotFoundError(f"no run {run_id}")
            current = _to_record(row)
            if expected is not None and current.status is not expected:
                raise StaleRunError(
                    f"run {run_id} is {current.status.value}, expected {expected.value}"
                )
            changed = RunRecord(
                **{**current.model_dump(), **updates, "status": to or current.status}
            )
            if "plan" in updates:
                self._save_plan(conn, current, changed)
            result = conn.execute(
                _UPDATE,
                {
                    "run_id": run_id,
                    "current": current.status.value,
                    "to": changed.status.value,
                    "document": changed.document,
                    "schema_id": changed.findings_schema_id,
                    "findings": None if changed.findings is None else _dumps(changed.findings),
                    "decisions": _dumps(
                        {k: d.model_dump(mode="json") for k, d in changed.decisions.items()}
                    ),
                    "error": changed.error,
                    "completed_at": changed.completed_at,
                    "plan_version": changed.plan_version,
                },
            )
            if result.rowcount != 1:
                raise StaleRunError(f"run {run_id} changed while it was being updated")
            return changed

    @staticmethod
    def _save_plan(conn: Any, current: RunRecord, changed: RunRecord) -> None:
        if changed.plan is None or changed.plan_version <= current.plan_version:
            raise ValueError("a new plan needs a plan_version higher than the current one")
        try:
            with conn.begin_nested():
                conn.execute(
                    _INSERT_PLAN,
                    {
                        "run_id": changed.run_id,
                        "version": changed.plan_version,
                        "steps": _dumps(changed.plan.model_dump(mode="json")["steps"]),
                        "model": changed.planner_model or "unknown",
                    },
                )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
                raise StaleRunError(f"plan version {changed.plan_version} already exists") from exc
            raise
