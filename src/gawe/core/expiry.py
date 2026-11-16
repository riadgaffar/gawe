"""Grant expiry jobs: created with a grant's dispatch intent, due at the grant's end date.

The job is added before the read-back intent, so no crash can leave a dispatched grant without
its expiry job. A job for a grant that never got dispatched simply finds nothing to revoke.

Revocation narrows access only. It is trusted maintenance, not a policy-authorized action, so it
needs no decision or approval; it is audited on the grant's run.
"""

from datetime import datetime, timedelta
from typing import Literal, Protocol

from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.maintenance import MaintenanceJob
from gawe.core.readback import operation_identity
from gawe.core.records import StepRecord
from gawe.core.runs import RunRecord

KIND = "grant_expiry"
# After this the job still retries every tick; by then the overdue alarm is already firing.
GRACE = timedelta(days=1)

GrantState = Literal["absent", "active", "expired", "revoked"]


class Revoker(Protocol):
    """The target system's revocation, keyed by the grant's operation key."""

    def revoke(self, revoke_key: str, grant_key: str, now: datetime) -> None:
        """Idempotent: revoking twice records once."""
        ...

    def grant_state(self, grant_key: str, now: datetime) -> GrantState: ...

    def overdue(self, now: datetime) -> int:
        """Grants past their end date that nothing has revoked."""
        ...


def revoke_identity(grant_key: str) -> str:
    return sha256_hex(canonical_json({"domain": "gawe.revoke.v1", "grant_key": grant_key}))


def expiry_identity(tenant_id: str, run_id: str, step_id: str, grant_key: str) -> str:
    return sha256_hex(
        canonical_json(
            {
                "domain": "gawe.grant_expiry.v1",
                "tenant_id": tenant_id,
                "run_id": run_id,
                "step_id": step_id,
                "grant_key": grant_key,
            }
        )
    )


def expiry_job(step: StepRecord, run: RunRecord, now: datetime) -> MaintenanceJob:
    """The expiry job for a grant about to be dispatched. Its id is deterministic, so adding it
    again (a retry, a second worker) is a no-op."""
    assert step.action_hash is not None
    grant_key = operation_identity(step.action_hash, run.plan_version)
    expires_at = datetime.fromisoformat(str(step.resolved_args["expires_at"]))
    due = max(expires_at, now)
    return MaintenanceJob(
        job_id=expiry_identity(run.tenant_id, run.run_id, step.step_id, grant_key),
        kind=KIND,
        tenant_id=run.tenant_id,
        run_id=run.run_id,
        plan_version=run.plan_version,
        step_id=step.step_id,
        action_hash=step.action_hash,
        operation_key=revoke_identity(grant_key),
        created_at=now,
        deadline=due + GRACE,
        due_at=due,
        payload={
            "tool": step.tool,
            "version": step.tool_version,
            "grant_key": grant_key,
            "expires_at": expires_at.isoformat(),
        },
    )
