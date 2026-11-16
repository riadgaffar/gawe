"""Periodic ticks inside the API process, started and stopped by the ASGI lifespan.

The first tick runs at startup and then every `interval` seconds, whether or not requests
arrive. Ticks run one at a time on a dedicated thread so blocking store and mock-system calls
never stall the event loop. `stop()` stops scheduling, waits for a tick in progress to finish,
and shuts the thread down. Ticks are expected to contain their own failures; anything that
escapes is logged and the schedule continues.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("gawe.maintenance")


class PeriodicTicker:
    def __init__(self, tick: Callable[[], object], *, interval: float) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._tick = tick
        self._interval = interval
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._pool: ThreadPoolExecutor | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("already started")
        self._stop = asyncio.Event()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gawe-maintenance")
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None or self._stop is None or self._pool is None:
            return
        self._stop.set()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task  # a tick in progress finishes; no new one starts
        self._pool.shutdown(wait=True)

    async def _loop(self) -> None:
        assert self._stop is not None and self._pool is not None
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                await loop.run_in_executor(self._pool, self._tick)
            except Exception:
                log.exception("maintenance tick escaped its own error handling")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
