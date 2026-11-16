"""The read-back job kind: resolve dispatched writes through the shared S3 resolver.

The handler has no executor and no tool implementations, so it has no path to a write. It reads
through `VerificationResolver.resolve` under the runner's claimed lease, then asks the run's
driver to settle and continue the already materialized plan. Continuation uses the orchestrator's
normal gates: terminal runs (cancelled, failed, unknown, denied) are only acknowledged, never
resumed, and a run that has not been planned is never handed to the planner from here.

`continue_run` may drive the run inline (the one-shot sweep) or only schedule it (the API hands it
to its background pool, so a slow next step never delays a tick or its heartbeat). Until the
driver acknowledges continuation the job stays due; the runner moves it back one interval, and a
repeated hand-off is harmless because the orchestrator drives each run once at a time.
"""

from collections.abc import Callable

from gawe.core.maintenance import MaintenanceJob, run_target
from gawe.core.statemachine import RunStatus
from gawe.core.verification import READBACK_KIND, VerificationResolver

KIND = READBACK_KIND
_RESOLVED = {
    "agreement": "done",
    "disagreement": "blocked",
    "unknown": "newly_unknown",
    "protocol_failure": "failed",
    "not_dispatched": "failed",
    "recipient_changed": "refreshed",
}
_UNPLANNED = frozenset({RunStatus.CREATED, RunStatus.PLANNING})


class ReadbackJobs:
    kind = KIND

    def __init__(
        self, resolver: VerificationResolver, continue_run: Callable[[str], object]
    ) -> None:
        self._resolver = resolver
        self._continue = continue_run

    def process(self, job: MaintenanceJob) -> str:
        r = self._resolver
        target = run_target(job)
        run = r.runs.get(target.run_id)
        if run.status in _UNPLANNED or run.plan_version != target.plan_version:
            return "skipped"
        before = job.result
        if before is None:
            step = r.steps.get(target.run_id, target.step_id)
            if step.action_hash != target.action_hash:
                return "skipped"  # the resolver selects jobs by the step's current action
            r.resolve(step, owned=job)  # read-only; raises JobConflictError if superseded
        current = r.jobs.get(job.tenant_id, job.job_id)
        assert current is not None
        if current.result is None:
            if run.status in {RunStatus.RUNNING, RunStatus.PAUSED_FOR_APPROVAL}:
                self._continue(target.run_id)  # a crash left the run unsettled; mark it waiting
            return "still_waiting"
        if not current.continued:
            self._continue(target.run_id)  # settle, continue the plan, acknowledge
            current = r.jobs.get(job.tenant_id, job.job_id)
            assert current is not None
        if before is None:
            return _RESOLVED[current.result["outcome"]] if current.result else "still_waiting"
        return "continued" if current.continued else "pending_continuation"
