"""Postgres CAS job storage, including completed work awaiting step/run continuation."""

import json
from datetime import datetime

from sqlalchemy import text

from gawe.core.maintenance import (
    IMMUTABLE,
    JobConflictError,
    MaintenanceJob,
    validate_job_change,
)
from gawe.db.bind import Bind, connection_for


class PostgresMaintenanceStore:
    def __init__(self, bind: Bind) -> None:
        self._bind = bind

    def add(self, job: MaintenanceJob) -> bool:
        with connection_for(self._bind) as conn:
            result = conn.execute(
                text(
                    "INSERT INTO maintenance_jobs (job_id, tenant_id, run_id, revision, due_at, "
                    "lease_until, continued, record) VALUES (:id, CAST(:tenant AS uuid), "
                    "CAST(:run AS uuid), :revision, :due, :lease, :continued, "
                    "CAST(:record AS jsonb)) "
                    "ON CONFLICT (job_id) DO NOTHING"
                ),
                self._params(job),
            )
            if result.rowcount == 1:
                return True
            raw = conn.execute(
                text("SELECT record FROM maintenance_jobs WHERE job_id=:id"), {"id": job.job_id}
            ).scalar_one()
            old = MaintenanceJob.model_validate(raw)
            if any(getattr(old, k) != getattr(job, k) for k in IMMUTABLE):
                raise JobConflictError("conflicting job intent")
            return False

    def get(self, tenant_id: str, job_id: str) -> MaintenanceJob | None:
        with connection_for(self._bind) as conn:
            raw = conn.execute(
                text(
                    "SELECT record FROM maintenance_jobs WHERE tenant_id=CAST(:tenant AS uuid) "
                    "AND job_id=:id"
                ),
                {"tenant": tenant_id, "id": job_id},
            ).scalar_one_or_none()
            return MaintenanceJob.model_validate(raw) if raw is not None else None

    def for_run(self, tenant_id: str, run_id: str) -> list[MaintenanceJob]:
        with connection_for(self._bind) as conn:
            rows = conn.execute(
                text(
                    "SELECT record FROM maintenance_jobs WHERE tenant_id=CAST(:tenant AS uuid) "
                    "AND run_id=CAST(:run AS uuid) ORDER BY job_id"
                ),
                {"tenant": tenant_id, "run": run_id},
            ).scalars()
            return [MaintenanceJob.model_validate(raw) for raw in rows]

    def due(
        self,
        now: datetime,
        *,
        limit: int = 200,
        after: tuple[datetime, str] | None = None,
    ) -> list[MaintenanceJob]:
        cursor = "AND (due_at, job_id) > (:after_due, :after_id) " if after else ""
        with connection_for(self._bind) as conn:
            rows = conn.execute(
                text(
                    "SELECT record FROM maintenance_jobs WHERE NOT continued AND due_at<=:now "
                    "AND (lease_until IS NULL OR lease_until<=:now) "
                    f"{cursor}ORDER BY due_at, job_id LIMIT :limit"
                ),
                {
                    "now": now,
                    "limit": max(0, limit),
                    "after_due": after[0] if after else None,
                    "after_id": after[1] if after else None,
                },
            ).scalars()
            return [MaintenanceJob.model_validate(raw) for raw in rows]

    def counts(self, kind: str) -> dict[str, int]:
        with connection_for(self._bind) as conn:
            rows = conn.execute(
                text(
                    "SELECT COALESCE(record->'result'->>'outcome', 'unresolved') AS outcome, "
                    "count(*) FROM maintenance_jobs WHERE record->>'kind'=:kind "
                    "GROUP BY outcome"
                ),
                {"kind": kind},
            ).all()
            return {str(outcome): int(total) for outcome, total in rows}

    def replace(self, job: MaintenanceJob, *, expected_revision: int) -> MaintenanceJob:
        with connection_for(self._bind) as conn:
            raw = conn.execute(
                text(
                    "SELECT record FROM maintenance_jobs WHERE job_id=:id AND revision=:revision "
                    "FOR UPDATE"
                ),
                {"id": job.job_id, "revision": expected_revision},
            ).scalar_one_or_none()
            if raw is None:
                raise JobConflictError("stale job revision")
            validate_job_change(MaintenanceJob.model_validate(raw), job)
            result = conn.execute(
                text(
                    "UPDATE maintenance_jobs SET revision=:revision, due_at=:due, "
                    "lease_until=:lease, "
                    "continued=:continued, record=CAST(:record AS jsonb) "
                    "WHERE job_id=:id AND revision=:expected"
                ),
                {**self._params(job), "expected": expected_revision},
            )
            if result.rowcount != 1:
                raise JobConflictError("stale job revision")
            return job

    @staticmethod
    def _params(job: MaintenanceJob) -> dict[str, object]:
        return {
            "id": job.job_id,
            "tenant": job.tenant_id,
            "run": job.run_id,
            "revision": job.revision,
            "due": job.due_at,
            "lease": job.lease_until,
            "continued": job.continued,
            "record": json.dumps(job.model_dump(mode="json")),
        }
