"""Ending grants made before M16b (M21): what the `end-old-grants` command plans and applies.

A dry run lists each old grant GAWE sent and the end date it would get, and writes nothing.
Applying adds M16b's `grant_expiry` job for each, due at that date, then audits
`grant.end_date_set` on the grant's run. The sweep does the rest, as for any grant: it revokes at
the end date and finishes when the IAM system reads the grant back as revoked or never granted.

Running it again changes nothing for a grant that already has its job: the job keeps the date it
was given, and the event is written once (from the stored job, so a crash between the job and the
event is repaired by the next run). Output and audit carry ids, dates and the rule, never the
grant's email, system or level.
"""

from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from gawe.core.anchor import Anchorer
from gawe.core.audit import AuditLog
from gawe.core.maintenance import JobConflictError, MaintenanceJob, MaintenanceStore
from gawe.core.old_grants import (
    OldGrant,
    Rule,
    end_date,
    find_old_grants,
    job_id,
    notice_period,
    old_grant_job,
)
from gawe.core.records import ExecutionStore, StepStore
from gawe.core.runs import RunStore
from gawe.core.statemachine import RUN_TERMINAL

ACTOR = "gawe.maintenance"
EVENT = "grant.end_date_set"


@dataclass(frozen=True)
class Planned:
    grant: OldGrant
    end: datetime
    rule: Rule
    job_exists: bool

    def line(self) -> dict[str, Any]:
        """One line of output: ids, the date and the rule. Never the grant's arguments."""
        return {
            "tenant_id": self.grant.tenant_id,
            "run_id": self.grant.run_id,
            "step_id": self.grant.step_id,
            "made_at": self.grant.made_at.isoformat(),
            "end_date": self.end.isoformat(),
            "rule": self.rule,
            "job": "exists" if self.job_exists else "new",
        }


class OldGrantBackfill:
    def __init__(
        self,
        *,
        versions: Iterable[tuple[str, str]],
        lifetime: timedelta,
        steps: StepStore,
        executions: ExecutionStore,
        runs: RunStore,
        jobs: MaintenanceStore,
        audit: AuditLog,
        anchorer: Anchorer,
        clock: Callable[[], datetime],
    ) -> None:
        self._versions = tuple(versions)
        self._lifetime = lifetime
        self._steps, self._executions, self._runs = steps, executions, runs
        self._jobs, self._audit, self._anchorer = jobs, audit, anchorer
        self._clock = clock

    def plan(self, notice_days: int) -> list[Planned]:
        """What applying would do now. Writes nothing."""
        notice = notice_period(notice_days)
        now = self._clock()
        planned: list[Planned] = []
        for grant in find_old_grants(
            self._versions, steps=self._steps, executions=self._executions, runs=self._runs
        ):
            existing = self._jobs.get(grant.tenant_id, job_id(grant))
            if existing is not None:
                planned.append(Planned(grant, *_set_by(existing), job_exists=True))
                continue
            end, rule = end_date(
                grant.made_at, lifetime=self._lifetime, backfill_at=now, notice=notice
            )
            planned.append(Planned(grant, end, rule, job_exists=False))
        return planned

    def apply(self, notice_days: int) -> list[Planned]:
        """Add each missing job, then audit each grant's end date once."""
        applied: list[Planned] = []
        for item in self.plan(notice_days):
            job = self._job_for(item, notice_days)
            self._record(item.grant, job)
            end, rule = _set_by(job)
            applied.append(Planned(item.grant, end, rule, job_exists=item.job_exists))
        return applied

    def _job_for(self, item: Planned, notice_days: int) -> MaintenanceJob:
        grant = item.grant
        if not item.job_exists:
            job = old_grant_job(
                grant, end=item.end, rule=item.rule, notice_days=notice_days, now=self._clock()
            )
            # Another backfill may have added it first, with its own date: keep that one.
            with suppress(JobConflictError):
                self._jobs.add(job)
        stored = self._jobs.get(grant.tenant_id, job_id(grant))
        assert stored is not None
        return stored

    def _record(self, grant: OldGrant, job: MaintenanceJob) -> None:
        before = self._audit.last(grant.run_id)
        backfill = job.payload.get("backfill", {})
        event = self._audit.record(
            grant.run_id,
            EVENT,
            ACTOR,
            {
                "step_id": grant.step_id,
                "grant_key": grant.grant_key,
                "end_date": job.payload["expires_at"],
                "rule": backfill.get("rule"),
                "notice_days": backfill.get("notice_days"),
            },
            once=True,  # a second run, or one after a crash, finds the first event
        )
        if before is None or event.seq > before.seq:
            run = self._runs.get(grant.run_id)
            if run.status in RUN_TERMINAL:
                self._anchorer.anchor_run(run)  # extends the run's anchor past the new event


def _set_by(job: MaintenanceJob) -> tuple[datetime, Rule]:
    """The end date and rule an existing job was given. A grant_expiry job made at dispatch
    (a current grant) has no backfill record; its rule is its own lifetime."""
    rule: Rule = "notice" if job.payload.get("backfill", {}).get("rule") == "notice" else "lifetime"
    return datetime.fromisoformat(str(job.payload["expires_at"])), rule
