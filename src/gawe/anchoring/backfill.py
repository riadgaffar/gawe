"""Anchor finished runs that a sink missed (it was down, or anchoring was turned on later)."""

import logging

from gawe.core.anchor import Anchorer
from gawe.core.runs import RunStore
from gawe.core.statemachine import RunStatus

log = logging.getLogger("gawe.anchor")

FINISHED_STATUSES = (
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.DENIED,
    RunStatus.CANCELLED,
)
DEFAULT_LIMIT = 10_000


def backfill(runs: RunStore, anchorer: Anchorer, *, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Fill in missing anchors for finished runs, oldest first. Safe to run any number of times.

    Returns the ids of runs that got at least one new anchor. One run failing does not stop the
    others.
    """
    filled: list[str] = []
    for run in runs.list_by_status(FINISHED_STATUSES, limit=limit):
        try:
            written = anchorer.fill_missing(run)
        except Exception:
            log.exception("could not anchor run %s", run.run_id)
            continue
        if written:
            filled.append(run.run_id)
    return filled
