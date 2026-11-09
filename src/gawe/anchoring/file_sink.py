"""A local, append-only file of anchor records (one JSON line each).

Not external: whoever controls this machine can edit the file. It is useful offline, in tests and
demos, and as a second copy next to a signed timestamp. Keep it on different storage from the
database if it is meant to protect against the database owner.

The file is re-read on every call, so a backfill script and the API can share it. Writes from two
processes are not coordinated; if both anchor one run differently, every later read of that run
fails with a conflict instead of picking one.
"""

import json
import os
import threading
from pathlib import Path

from gawe.core.anchor import AnchorConflictError, AnchorFormatError, AnchorRecord, EvidenceCheck


class FileAnchorSink:
    name = "file"
    external = False

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def put(self, record: AnchorRecord) -> AnchorRecord:
        if record.sink != self.name:
            raise ValueError(f"record is for sink {record.sink!r}, not {self.name!r}")
        with self._lock:
            existing = self._read().get(record.run_id)
            if existing is not None:
                if existing.digest != record.digest:
                    raise AnchorConflictError(f"run {record.run_id} already has a different anchor")
                return existing
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_json(), sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return record

    def get(self, run_id: str) -> AnchorRecord | None:
        with self._lock:
            return self._read().get(run_id)

    def check(self, record: AnchorRecord) -> EvidenceCheck:
        if not record.digest_ok():
            return EvidenceCheck(
                ok=False, external=False, reason="digest does not match the record"
            )
        return EvidenceCheck(ok=True, external=False)

    def _read(self) -> dict[str, AnchorRecord]:
        """Raises AnchorFormatError on a malformed line and AnchorConflictError if two lines give
        one run different anchors, so a tampered file is never silently trusted."""
        records: dict[str, AnchorRecord] = {}
        if not self._path.exists():
            return records
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except ValueError as exc:
                raise AnchorFormatError("anchor file has a line that is not JSON") from exc
            if not isinstance(data, dict):
                raise AnchorFormatError("anchor file has a line that is not an object")
            record = AnchorRecord.from_json(data)
            existing = records.setdefault(record.run_id, record)
            if existing.digest != record.digest:
                raise AnchorConflictError(f"anchor file holds two anchors for run {record.run_id}")
        return records
