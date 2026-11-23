"""Grants made before M16b, and the end date GAWE gives each (M21).

They came from a retired grant version with no end date. GAWE finds them in its own records, not
in the target system: a step of that version that GAWE sent (it succeeded, or its outcome was
left unknown or blocked) and the execution that sent it. The execution's idempotency key is the
key the target system knows the grant by, in every era; its finish time (or start, if it never
finished) is when the grant was made. Grants GAWE didn't make are never touched.

The end date is the later of two: the grant's time plus the lifetime the app grants today, and
the backfill's time plus a notice period. No old grant outlasts a new one made the same day,
except by the notice, and nobody loses access without warning.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from gawe.core.expiry import GRACE, KIND, expiry_identity, revoke_identity
from gawe.core.maintenance import MaintenanceJob
from gawe.core.records import ExecutionRecord, ExecutionStore, StepStore
from gawe.core.runs import RunNotFoundError, RunStore
from gawe.core.statemachine import StepStatus

DEFAULT_NOTICE_DAYS = 7
NOTICE_DAYS_RANGE = (1, 30)
# What GAWE sent: done, or sent with an outcome nobody could confirm. The job revokes only what
# the target reads as active, so a grant that never landed ends as "not granted".
SENT = frozenset(
    {StepStatus.SUCCEEDED, StepStatus.VERIFICATION_UNKNOWN, StepStatus.VERIFICATION_BLOCKED}
)

Rule = Literal["lifetime", "notice"]


@dataclass(frozen=True)
class OldGrant:
    tenant_id: str
    run_id: str
    step_id: str
    tool: str
    version: str
    grant_key: str  # the key the target system knows the grant by
    made_at: datetime
    step_status: str
    action_hash: str
    plan_version: int


def notice_period(days: int) -> timedelta:
    low, high = NOTICE_DAYS_RANGE
    if not low <= days <= high:
        raise ValueError(f"the notice period must be {low} to {high} days, not {days}")
    return timedelta(days=days)


def end_date(
    made_at: datetime, *, lifetime: timedelta, backfill_at: datetime, notice: timedelta
) -> tuple[datetime, Rule]:
    """The later of the grant's natural end and the notice date, and which one it was. A tie is
    the natural end: the grant ends when a new one would have."""
    natural, noticed = made_at + lifetime, backfill_at + notice
    return (natural, "lifetime") if natural >= noticed else (noticed, "notice")


def _sending_attempt(attempts: list[ExecutionRecord]) -> ExecutionRecord | None:
    """The attempt that sent the grant: the succeeded one, else the latest (an outcome left
    unknown). None when nothing was ever sent."""
    succeeded = [a for a in attempts if a.status == "succeeded"]
    if succeeded:
        return succeeded[-1]
    return attempts[-1] if attempts else None


def find_old_grants(
    versions: Iterable[tuple[str, str]],
    *,
    steps: StepStore,
    executions: ExecutionStore,
    runs: RunStore,
) -> list[OldGrant]:
    """Every grant GAWE sent with one of these (retired) tool versions, by tool, run and step."""
    found: list[OldGrant] = []
    for tool, version in sorted(set(versions)):
        for step in steps.list_for_tool(tool, version):
            if step.status not in SENT:
                continue  # failed, refused or never dispatched: GAWE granted nothing
            attempt = _sending_attempt(executions.attempts(step.run_id, step.step_id))
            if attempt is None:
                continue
            try:
                run = runs.get(step.run_id)
            except RunNotFoundError:
                continue
            found.append(
                OldGrant(
                    tenant_id=run.tenant_id,
                    run_id=step.run_id,
                    step_id=step.step_id,
                    tool=tool,
                    version=version,
                    grant_key=attempt.idempotency_key,
                    made_at=attempt.finished_at or attempt.started_at,
                    step_status=step.status.value,
                    action_hash=str(step.action_hash),
                    plan_version=run.plan_version or 1,
                )
            )
    return found


def job_id(grant: OldGrant) -> str:
    """The same identity a grant_expiry job has, from what doesn't change: so a later backfill,
    with another date, finds the job the first one made instead of adding a second."""
    return expiry_identity(grant.tenant_id, grant.run_id, grant.step_id, grant.grant_key)


def old_grant_job(
    grant: OldGrant, *, end: datetime, rule: Rule, notice_days: int, now: datetime
) -> MaintenanceJob:
    """M16b's grant_expiry job, due at the end date GAWE set. `backfill` records how it was set."""
    return MaintenanceJob(
        job_id=job_id(grant),
        kind=KIND,
        tenant_id=grant.tenant_id,
        run_id=grant.run_id,
        plan_version=grant.plan_version,
        step_id=grant.step_id,
        action_hash=grant.action_hash,
        operation_key=revoke_identity(grant.grant_key),
        created_at=now,
        deadline=max(end, now) + GRACE,
        due_at=end,
        payload={
            "tool": grant.tool,
            "version": grant.version,
            "grant_key": grant.grant_key,
            "expires_at": end.isoformat(),
            "backfill": {"rule": rule, "notice_days": notice_days},
        },
    )


def is_old_grant_job(job: MaintenanceJob) -> bool:
    return job.kind == KIND and "backfill" in job.payload
