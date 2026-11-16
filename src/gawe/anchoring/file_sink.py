"""A local, append-only file of anchor records (one JSON line each).

Not external: whoever controls this machine can edit the file. It is useful offline, in tests and
demos, and as a second copy next to a signed timestamp. Keep it on different storage from the
database if it is meant to protect against the database owner.

The file is re-read on every call. put() remains write-once; explicit continuations append a
checkpoint linked to its predecessor. Writes from independent processes are not coordinated:
forked histories fail closed on reload rather than silently picking one.
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

    def extend(self, record: AnchorRecord, *, previous: AnchorRecord) -> AnchorRecord:
        """Append an explicitly verified continuation; ordinary put remains write-once.

        Anchorer checks the live audit prefix and earlier evidence before requesting this CAS.
        The predecessor digest makes forks, gaps and history truncation detectable on reload.
        """
        if (
            record.sink != self.name
            or record.run_id != previous.run_id
            or record.tenant_id != previous.tenant_id
            or not previous.digest_ok()
            or not record.digest_ok()
            or record.point.seq <= previous.point.seq
        ):
            raise AnchorConflictError("invalid anchor continuation")
        with self._lock:
            existing = self._read().get(record.run_id)
            if existing is not None and existing.digest == record.digest:
                return existing
            if existing is None or existing.digest != previous.digest:
                raise AnchorConflictError("anchor changed before continuation")
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {**record.to_json(), "previous_digest": previous.digest}, sort_keys=True
                    )
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            return record

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
            existing = records.get(record.run_id)
            predecessor = data.get("previous_digest")
            if existing is None:
                if predecessor is not None:
                    raise AnchorConflictError("anchor continuation has no predecessor")
                records[record.run_id] = record
            elif existing.digest != record.digest:
                if (
                    predecessor != existing.digest
                    or record.point.seq <= existing.point.seq
                    or record.tenant_id != existing.tenant_id
                    or not existing.digest_ok()
                    or not record.digest_ok()
                ):
                    raise AnchorConflictError(
                        f"anchor file holds conflicting history for {record.run_id}"
                    )
                records[record.run_id] = record
        return records
