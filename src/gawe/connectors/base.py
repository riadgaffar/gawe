"""Shared pieces of the mock connectors: an append-only ledger and fault injection."""

import json
import os
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gawe.core.executor import ToolTransientError


class ConnectorError(Exception):
    """A permanent failure. `code` is a short label that is safe to store and show."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TransientConnectorError(ToolTransientError):
    """A failure worth retrying (a timeout, a 503)."""

    code = "transient"


class FaultInjector:
    """Makes the next N calls fail, to demonstrate retries and failure handling."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._transient = 0
        self._permanent = 0

    def fail_next(self, count: int = 1, *, transient: bool = True) -> None:
        with self._lock:
            if transient:
                self._transient += count
            else:
                self._permanent += count

    def check(self) -> None:
        with self._lock:
            if self._transient > 0:
                self._transient -= 1
                raise TransientConnectorError("simulated outage")
            if self._permanent > 0:
                self._permanent -= 1
                raise ConnectorError("simulated_rejection")


class Ledger:
    """Append-only record of effects, one per idempotency key.

    With a path, every record is written as one JSON line and flushed to disk before the call
    returns, and the index is rebuilt from the file on start, so a restarted process still
    recognizes work it already did. Without a path it lives in memory.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._by_key: dict[tuple[str, str], dict[str, Any]] = {}
        if path is not None and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._index(json.loads(line))

    def record_once(
        self, kind: str, key: str, build: Callable[[], Mapping[str, Any]]
    ) -> tuple[dict[str, Any], bool]:
        """Return (record, created). A repeated (kind, key) returns the original, unchanged."""
        with self._lock:
            existing = self._by_key.get((kind, key))
            if existing is not None:
                return existing, False
            record = {
                "kind": kind,
                "key": key,
                "at": datetime.now(UTC).isoformat(),
                "data": dict(build()),
            }
            if self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            self._index(record)
            return record, True

    def records(self, kind: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self._records if kind is None or r["kind"] == kind]

    def _index(self, record: dict[str, Any]) -> None:
        self._records.append(record)
        self._by_key[(record["kind"], record["key"])] = record
