"""Runs orchestrator work off the request thread.

`advance` can take a while (it calls the LLM and the tools), so requests start it and return;
clients follow progress through the audit event stream. Work is tied to a run id and is safe to
repeat: the orchestrator ignores a second driver for a run that is already being advanced.
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

log = logging.getLogger("gawe.api")


class Background(Protocol):
    def submit(self, job: Callable[[], object]) -> None: ...


class InlineBackground:
    """Runs the job immediately on the caller's thread. For tests and simple scripts."""

    def submit(self, job: Callable[[], object]) -> None:
        job()


class ThreadBackground:
    def __init__(self, max_workers: int = 4) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="gawe-run")

    def submit(self, job: Callable[[], object]) -> None:
        self._pool.submit(self._guard, job)

    @staticmethod
    def _guard(job: Callable[[], object]) -> None:
        try:
            job()
        except Exception:
            log.exception("background job failed")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)
