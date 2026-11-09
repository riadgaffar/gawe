"""Postgres implementation of ExecutionStore.

* `start` inserts attempt N only if N is exactly one more than the highest existing attempt;
  UNIQUE (step_id, attempt) makes two workers racing for the same number collide, and the loser
  gets AttemptConflictError.
* `finish` closes an attempt only while it is still `started`.
* `save_output` is write-once (`ON CONFLICT DO NOTHING`, plus an UPDATE-blocking trigger), so the
  output a verifier checks is the output the tool first returned.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.records import AttemptConflictError, ExecutionRecord, StepNotFoundError
from gawe.db.bind import Bind, connection_for

_UNIQUE_VIOLATION = "23505"

_CURRENT_STEP = (
    "FROM steps s JOIN runs r ON r.id = s.run_id "
    "JOIN plans p ON p.id = s.plan_id AND p.version = r.current_plan_version "
    "WHERE s.run_id = CAST(:run_id AS uuid) AND s.step_key = :step_key"
)
_STEP_ID = text(f"SELECT s.id {_CURRENT_STEP}")
_ATTEMPTS = text(
    "SELECT CAST(s.run_id AS text) AS run_id, s.step_key AS step_id, e.attempt, "
    "e.idempotency_key, e.status, e.error, e.output_hash, e.started_at, e.finished_at "
    "FROM executions e JOIN steps s ON s.id = e.step_id "
    "JOIN runs r ON r.id = s.run_id "
    "JOIN plans p ON p.id = s.plan_id AND p.version = r.current_plan_version "
    "WHERE s.run_id = CAST(:run_id AS uuid) AND s.step_key = :step_key ORDER BY e.attempt"
)
_START = text(
    "INSERT INTO executions (step_id, attempt, idempotency_key, status, started_at) "
    "SELECT CAST(:step_id AS uuid), CAST(:attempt AS integer), CAST(:key AS text), 'started', "
    "CAST(:now AS timestamptz) "
    "WHERE CAST(:attempt AS integer) = "
    "COALESCE((SELECT max(attempt) FROM executions WHERE step_id = CAST(:step_id AS uuid)), 0) + 1"
)
_FINISH = text(
    "UPDATE executions SET status = :status, finished_at = :now, error = :error, "
    "output_hash = :output_hash "
    "WHERE step_id = CAST(:step_id AS uuid) AND attempt = :attempt AND status = 'started' "
    "RETURNING idempotency_key, started_at"
)
_SAVE_OUTPUT = text(
    "INSERT INTO step_outputs (step_id, output, output_hash) "
    "VALUES (CAST(:step_id AS uuid), CAST(:output AS jsonb), :output_hash) "
    "ON CONFLICT (step_id) DO NOTHING"
)
_LOAD_OUTPUT = text(
    "SELECT o.output FROM step_outputs o JOIN steps s ON s.id = o.step_id "
    "JOIN runs r ON r.id = s.run_id "
    "JOIN plans p ON p.id = s.plan_id AND p.version = r.current_plan_version "
    "WHERE s.run_id = CAST(:run_id AS uuid) AND s.step_key = :step_key"
)


class PostgresExecutionStore:
    """Pass a Connection to join the caller's transaction, or an Engine to commit per call."""

    def __init__(self, bind: Bind) -> None:
        self._bind = bind

    def _step_id(self, conn: Any, run_id: str, step_id: str) -> Any:
        found = conn.execute(_STEP_ID, {"run_id": run_id, "step_key": step_id}).scalar()
        if found is None:
            raise StepNotFoundError(f"no step {step_id} in run {run_id}")
        return found

    def attempts(self, run_id: str, step_id: str) -> list[ExecutionRecord]:
        with connection_for(self._bind) as conn:
            rows = conn.execute(_ATTEMPTS, {"run_id": run_id, "step_key": step_id}).mappings().all()
        return [ExecutionRecord(**dict(row)) for row in rows]

    def start(
        self, run_id: str, step_id: str, *, attempt: int, idempotency_key: str, now: datetime
    ) -> ExecutionRecord:
        if attempt < 1:
            raise AttemptConflictError(f"attempt {attempt} is not valid")
        with connection_for(self._bind) as conn:
            sid = self._step_id(conn, run_id, step_id)
            try:
                with conn.begin_nested():
                    result = conn.execute(
                        _START,
                        {"step_id": sid, "attempt": attempt, "key": idempotency_key, "now": now},
                    )
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
                    raise AttemptConflictError(f"attempt {attempt} already exists") from exc
                raise
            if result.rowcount != 1:
                raise AttemptConflictError(f"attempt {attempt} is not the next attempt")
        return ExecutionRecord(
            run_id=run_id,
            step_id=step_id,
            attempt=attempt,
            idempotency_key=idempotency_key,
            status="started",
            started_at=now,
        )

    def finish(
        self,
        run_id: str,
        step_id: str,
        *,
        attempt: int,
        status: Literal["succeeded", "failed"],
        now: datetime,
        error: str | None = None,
        output_hash: str | None = None,
    ) -> ExecutionRecord:
        with connection_for(self._bind) as conn:
            sid = self._step_id(conn, run_id, step_id)
            row = (
                conn.execute(
                    _FINISH,
                    {
                        "step_id": sid,
                        "attempt": attempt,
                        "status": status,
                        "now": now,
                        "error": error,
                        "output_hash": output_hash,
                    },
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AttemptConflictError(f"attempt {attempt} is not in progress")
        return ExecutionRecord(
            run_id=run_id,
            step_id=step_id,
            attempt=attempt,
            idempotency_key=row["idempotency_key"],
            status=status,
            error=error,
            output_hash=output_hash,
            started_at=row["started_at"],
            finished_at=now,
        )

    def save_output(self, run_id: str, step_id: str, output: Mapping[str, Any]) -> None:
        body = canonical_json(output)
        with connection_for(self._bind) as conn:
            sid = self._step_id(conn, run_id, step_id)
            conn.execute(
                _SAVE_OUTPUT,
                {"step_id": sid, "output": body.decode("utf-8"), "output_hash": sha256_hex(body)},
            )

    def load_output(self, run_id: str, step_id: str) -> dict[str, Any] | None:
        with connection_for(self._bind) as conn:
            value = conn.execute(_LOAD_OUTPUT, {"run_id": run_id, "step_key": step_id}).scalar()
        return None if value is None else dict(value)
