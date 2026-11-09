"""Limits repeated failed authentication from one client address.

Tokens are long random values, so guessing one is not realistic; this limiter is defence in depth
and keeps a noisy or hostile client from filling logs and spending CPU. It counts failures only:
successful requests are never counted or slowed. Once an address reaches the limit, all of its
requests get 429 until its oldest counted failure leaves the window.

State is in memory, per process, with a hard cap on tracked addresses. Behind the web UI every
browser user reaches the API from the UI server's address, so a flood of bad sign-ins can lock
the UI out for up to one window. Trusting a forwarded client address needs a trusted-proxy
setting, which is not built yet (see docs/threat-model.md).
"""

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable

DEFAULT_LIMIT = 20
DEFAULT_WINDOW_SECONDS = 300.0
DEFAULT_MAX_KEYS = 10_000


class FailureThrottle:
    def __init__(
        self,
        *,
        limit: int = DEFAULT_LIMIT,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0 or max_keys < 1:
            raise ValueError("limit, window_seconds and max_keys must be positive")
        self._limit = limit
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int | None:
        """Whole seconds to wait if `key` is blocked, else None."""
        with self._lock:
            now = self._clock()
            recent = self._recent(key, now)
            if len(recent) < self._limit:
                return None
            return max(1, int(recent[0] + self._window - now) + 1)

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            recent = self._recent(key, now)
            recent.append(now)
            self._failures[key] = recent
            self._failures.move_to_end(key)
            while len(self._failures) > self._max_keys:
                self._failures.popitem(last=False)  # forget the least recently failing address

    def _recent(self, key: str, now: float) -> deque[float]:
        recent = self._failures.get(key)
        if recent is None:
            return deque()
        while recent and recent[0] <= now - self._window:
            recent.popleft()
        if not recent:
            del self._failures[key]
        return recent
