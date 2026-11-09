"""Postgres implementation of StepStore.

A step is addressed by (run_id, step_key) and resolved through the run's *current* plan version,
so after a replan the same key refers to the new plan's step. Approvals and executions hang off
the step row's uuid, which means nothing carries over from an older plan version.

Every status change is a compare-and-set: the row is locked, its status compared with the
caller's `expected`, the new record validated by the same Pydantic model the in-memory store uses,
and the UPDATE repeats the status check. Exactly one concurrent caller wins; the rest get
StaleStateError.
"""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from gawe.core.records import StaleStateError, StepNotFoundError, StepRecord
from gawe.core.statemachine import StepStatus, check_step_transition
from gawe.db.bind import Bind, connection_for

_UPDATABLE = frozenset({"action_hash", "error", "resolved_args"})
_UNIQUE_VIOLATION = "23505"

_COLUMNS = (
    "CAST(s.run_id AS text) AS run_id, s.step_key, s.tool_name, s.tool_version, s.args_template, "
    "s.resolved_args, s.action_hash, s.depends_on, s.status, s.error"
)
# Steps of the run's current plan only.
_CURRENT = (
    "FROM steps s "
    "JOIN runs r ON r.id = s.run_id "
    "JOIN plans p ON p.id = s.plan_id AND p.version = r.current_plan_version "
    "WHERE s.run_id = CAST(:run_id AS uuid) AND s.step_key = :step_key"
)

_SELECT = text(f"SELECT {_COLUMNS} {_CURRENT}")
_SELECT_LOCKED = text(f"SELECT s.id, {_COLUMNS} {_CURRENT} FOR UPDATE OF s")
_PLAN = text(
    "SELECT p.id FROM plans p "
    "JOIN runs r ON r.id = p.run_id AND p.version = r.current_plan_version "
    "WHERE p.run_id = CAST(:run_id AS uuid)"
)
_INSERT = text(
    "INSERT INTO steps (run_id, plan_id, step_key, tool_name, tool_version, args_template, "
    "resolved_args, action_hash, depends_on, status, error) "
    "VALUES (CAST(:run_id AS uuid), CAST(:plan_id AS uuid), :step_key, :tool, :version, "
    "CAST(:args_template AS jsonb), CAST(:resolved_args AS jsonb), :action_hash, :depends_on, "
    ":status, :error)"
)
_UPDATE = text(
    "UPDATE steps SET status = :to, action_hash = :action_hash, error = :error, "
    "resolved_args = CAST(:resolved_args AS jsonb), updated_at = now() "
    "WHERE id = CAST(:id AS uuid) AND status = :expected"
)


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _to_record(row: Any) -> StepRecord:
    return StepRecord(
        run_id=row["run_id"],
        step_id=row["step_key"],
        tool=row["tool_name"],
        tool_version=row["tool_version"],
        args_template=row["args_template"] or {},
        resolved_args=row["resolved_args"] or {},
        action_hash=row["action_hash"],
        depends_on=tuple(row["depends_on"] or ()),
        status=StepStatus(row["status"]),
        error=row["error"],
    )


class PostgresStepStore:
    """Pass a Connection to join the caller's transaction, or an Engine to commit per call."""

    def __init__(self, bind: Bind) -> None:
        self._bind = bind

    def add(self, step: StepRecord) -> None:
        with connection_for(self._bind) as conn:
            plan_id = conn.execute(_PLAN, {"run_id": step.run_id}).scalar()
            if plan_id is None:
                raise ValueError(f"run {step.run_id} has no current plan")
            params = {
                "run_id": step.run_id,
                "plan_id": plan_id,
                "step_key": step.step_id,
                "tool": step.tool,
                "version": step.tool_version,
                "args_template": _dumps(step.args_template),
                "resolved_args": _dumps(step.resolved_args),
                "action_hash": step.action_hash,
                "depends_on": list(step.depends_on),
                "status": step.status.value,
                "error": step.error,
            }
            try:
                with conn.begin_nested():
                    conn.execute(_INSERT, params)
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
                    raise ValueError(
                        f"step {step.step_id} already exists in run {step.run_id}"
                    ) from exc
                raise

    def get(self, run_id: str, step_id: str) -> StepRecord:
        with connection_for(self._bind) as conn:
            row = conn.execute(_SELECT, {"run_id": run_id, "step_key": step_id}).mappings().first()
        if row is None:
            raise StepNotFoundError(f"no step {step_id} in run {run_id}")
        return _to_record(row)

    def transition(
        self, run_id: str, step_id: str, *, expected: StepStatus, to: StepStatus, **updates: Any
    ) -> StepRecord:
        unknown = set(updates) - _UPDATABLE
        if unknown:
            raise ValueError(f"cannot update fields: {sorted(unknown)}")
        check_step_transition(expected, to)
        with connection_for(self._bind) as conn:
            row = (
                conn.execute(_SELECT_LOCKED, {"run_id": run_id, "step_key": step_id})
                .mappings()
                .first()
            )
            if row is None:
                raise StepNotFoundError(f"no step {step_id} in run {run_id}")
            current = _to_record(row)
            if current.status is not expected:
                raise StaleStateError(
                    f"step {step_id} is {current.status.value}, expected {expected.value}"
                )
            # Same model validation as the in-memory store (e.g. a hash is required past policy).
            changed = StepRecord(**{**current.model_dump(), **updates, "status": to})
            result = conn.execute(
                _UPDATE,
                {
                    "id": row["id"],
                    "to": to.value,
                    "expected": expected.value,
                    "action_hash": changed.action_hash,
                    "error": changed.error,
                    "resolved_args": _dumps(changed.resolved_args),
                },
            )
            if result.rowcount != 1:
                raise StaleStateError(f"step {step_id} changed while it was being updated")
            return changed
