"""Restart recovery: pick up runs that a stopped process left unfinished.

`Orchestrator.advance` is re-entrant and the executor claims each step exactly once (compare and
set plus idempotency keys), so driving a run again is safe whatever state the crash left it in.
A run waiting for approval is included: if a person approved just before the restart, nothing
else would move it on. For one still waiting, `advance` simply finds it still waiting.
"""

import logging
from collections.abc import Callable

from gawe.core.runs import RunStore
from gawe.core.statemachine import RunStatus

log = logging.getLogger("gawe.recovery")

ACTIVE_STATUSES = (
    RunStatus.CREATED,
    RunStatus.PLANNING,
    RunStatus.RUNNING,
    RunStatus.PAUSED_FOR_APPROVAL,
)
DEFAULT_LIMIT = 200


def resume_interrupted(
    runs: RunStore, submit: Callable[[str], None], *, limit: int = DEFAULT_LIMIT
) -> list[str]:
    """Hand each unfinished run (oldest first, at most `limit`) to `submit`.

    One run failing to submit does not stop the others. Returns the ids that were submitted.
    """
    submitted: list[str] = []
    for run in runs.list_by_status(ACTIVE_STATUSES, limit=limit):
        try:
            submit(run.run_id)
        except Exception:
            log.exception("could not resume run %s", run.run_id)
            continue
        submitted.append(run.run_id)
    if submitted:
        log.info("resumed %d unfinished run(s)", len(submitted))
    return submitted
