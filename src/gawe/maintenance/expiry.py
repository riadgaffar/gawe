"""The grant-expiry job kind: revoke a grant at its end date and confirm it by reading back.

A job is done only when the target system reads the grant back as revoked, or as never granted.
A revoke the system does not reflect leaves the job waiting, and the grant counts as overdue in
every heartbeat until it is revoked. Revocation uses the job's own operation key, so a repeat
after a crash, a lost lease or a second sweeper records one revocation.

The order is the read-back resolver's: save the result, then audit `grant.revoked` on the grant's
run and keep the event's sequence, then refresh the run's anchor if the run has ended, then mark
the job continued. A crash repeats the bookkeeping, never the decision. The run's status does not
matter: grants from cancelled or failed runs are revoked too.
"""

from collections.abc import Callable, Mapping
from datetime import datetime

from gawe.core.anchor import Anchorer
from gawe.core.audit import AuditLog
from gawe.core.expiry import KIND, Revoker
from gawe.core.maintenance import MaintenanceJob, MaintenanceStore, run_target
from gawe.core.runs import RunStore
from gawe.core.statemachine import RUN_TERMINAL

ACTOR = "gawe.maintenance"
_DONE = {"revoked": "revoked", "absent": "not_granted"}


class GrantExpiryJobs:
    kind = KIND

    def __init__(
        self,
        *,
        revokers: Mapping[tuple[str, str], Revoker],
        jobs: MaintenanceStore,
        runs: RunStore,
        audit: AuditLog,
        anchorer: Anchorer,
        clock: Callable[[], datetime],
    ) -> None:
        self._revokers = dict(revokers)
        self._jobs, self._runs, self._audit, self._anchorer = jobs, runs, audit, anchorer
        self._clock = clock

    def overdue(self, now: datetime) -> int:
        """Grants past their end date that nothing revoked, read from each target system (not
        from the jobs, so a grant whose job is missing is counted too)."""
        unique = {id(r): r for r in self._revokers.values()}
        return sum(r.overdue(now) for r in unique.values())

    def process(self, job: MaintenanceJob) -> str:
        target = run_target(job)
        fresh = job.result is None
        if fresh:
            revoker = self._revokers.get((job.payload["tool"], job.payload["version"]))
            if revoker is None:
                return "skipped"  # another deployment's tool
            grant_key = str(job.payload["grant_key"])
            now = self._clock()
            revoker.revoke(job.operation_key, grant_key, now)
            state = revoker.grant_state(grant_key, self._clock())
            if state not in _DONE:
                return "still_waiting"  # not read back as revoked; stays overdue
            job = self._change(
                job,
                result={"outcome": _DONE[state], "code": f"grant_{state}"},
                evidence={"state": state, "observed_at": self._clock().isoformat()},
            )
        assert job.result is not None
        if job.evidence_seq is None:
            event = self._audit.record(
                target.run_id,
                "grant.revoked",
                ACTOR,
                {
                    "step_id": target.step_id,
                    "grant_key": job.payload["grant_key"],
                    "revoke_key": job.operation_key,
                    "outcome": job.result["outcome"],
                },
                once=True,  # a sweeper that lost its lease may get here too
            )
            job = self._change(job, evidence_seq=event.seq)
        if not job.continued:
            run = self._runs.get(target.run_id)
            if run.status in RUN_TERMINAL:
                self._anchorer.anchor_run(run)  # extends the run's anchor past the new event
            job = self._change(job, continued=True)
        assert job.result is not None
        return str(job.result["outcome"]) if fresh else "continued"

    def _change(self, job: MaintenanceJob, **updates: object) -> MaintenanceJob:
        changed = MaintenanceJob(**{**job.model_dump(), **updates, "revision": job.revision + 1})
        return self._jobs.replace(changed, expected_revision=job.revision)
