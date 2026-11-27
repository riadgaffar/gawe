"""Postgres undo records (M26): insert once, then compare-and-set by revision."""

import json

from sqlalchemy import text

from gawe.core.undo import IMMUTABLE, UndoConflictError, UndoRecord, validate_change
from gawe.db.bind import Bind, connection_for


class PostgresUndoStore:
    def __init__(self, bind: Bind) -> None:
        self._bind = bind

    def add(self, record: UndoRecord) -> bool:
        with connection_for(self._bind) as conn:
            result = conn.execute(
                text(
                    "INSERT INTO undo_records (undo_id, tenant_id, run_id, revision, state, "
                    "record) VALUES (:id, CAST(:tenant AS uuid), CAST(:run AS uuid), "
                    ":revision, :state, CAST(:record AS jsonb)) ON CONFLICT (undo_id) DO NOTHING"
                ),
                self._params(record),
            )
            if result.rowcount == 1:
                return True
            raw = conn.execute(
                text("SELECT record FROM undo_records WHERE undo_id=:id"),
                {"id": record.undo_id},
            ).scalar_one()
            old = UndoRecord.model_validate(raw)
            if any(getattr(old, k) != getattr(record, k) for k in IMMUTABLE):
                raise UndoConflictError("conflicting undo record")
            return False

    def get(self, tenant_id: str, undo_id: str) -> UndoRecord | None:
        with connection_for(self._bind) as conn:
            raw = conn.execute(
                text(
                    "SELECT record FROM undo_records WHERE tenant_id=CAST(:tenant AS uuid) "
                    "AND undo_id=:id"
                ),
                {"tenant": tenant_id, "id": undo_id},
            ).scalar_one_or_none()
            return UndoRecord.model_validate(raw) if raw is not None else None

    def for_run(self, tenant_id: str, run_id: str) -> list[UndoRecord]:
        with connection_for(self._bind) as conn:
            rows = conn.execute(
                text(
                    "SELECT record FROM undo_records WHERE tenant_id=CAST(:tenant AS uuid) "
                    "AND run_id=CAST(:run AS uuid) ORDER BY undo_id"
                ),
                {"tenant": tenant_id, "run": run_id},
            ).scalars()
            return [UndoRecord.model_validate(raw) for raw in rows]

    def counts(self, tenant_id: str) -> dict[tuple[str, str | None], int]:
        with connection_for(self._bind) as conn:
            rows = conn.execute(
                text(
                    "SELECT state, record->>'reason' AS reason, count(*) AS n FROM undo_records "
                    "WHERE tenant_id=CAST(:tenant AS uuid) GROUP BY state, record->>'reason'"
                ),
                {"tenant": tenant_id},
            )
            return {(row.state, row.reason): row.n for row in rows}

    def replace(self, record: UndoRecord, *, expected_revision: int) -> UndoRecord:
        with connection_for(self._bind) as conn:
            raw = conn.execute(
                text(
                    "SELECT record FROM undo_records WHERE undo_id=:id AND revision=:revision "
                    "FOR UPDATE"
                ),
                {"id": record.undo_id, "revision": expected_revision},
            ).scalar_one_or_none()
            if raw is None:
                raise UndoConflictError("undo record changed")
            validate_change(UndoRecord.model_validate(raw), record)
            result = conn.execute(
                text(
                    "UPDATE undo_records SET revision=:revision, state=:state, "
                    "record=CAST(:record AS jsonb) WHERE undo_id=:id AND revision=:expected"
                ),
                {**self._params(record), "expected": expected_revision},
            )
            if result.rowcount != 1:
                raise UndoConflictError("undo record changed")
            return record

    @staticmethod
    def _params(record: UndoRecord) -> dict[str, object]:
        return {
            "id": record.undo_id,
            "tenant": record.tenant_id,
            "run": record.run_id,
            "revision": record.revision,
            "state": record.state,
            "record": json.dumps(record.model_dump(mode="json")),
        }
