"""Where a benchmark run's output goes, written as it happens.

    bench-results/<run id>/manifest.json   how the run was set up (written first, never changed)
    bench-results/<run id>/trials.jsonl    one line per finished trial, flushed to disk at once
    bench-results/<run id>/status.json     complete or incomplete, why, and what was spent

A run that stops on the budget, or crashes, still leaves every trial it finished. Trial records
hold labels, counts and costs; never documents, prompts or model output.
"""

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from bench.budget import BudgetExceededError

RESULTS_DIR = Path("bench-results")
STATUS_BUDGET_STOPPED = "budget_stopped"


def _encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"cannot write {type(value).__name__} to results")


def _dumps(value: Any) -> str:
    return json.dumps(value, default=_encode, sort_keys=True)


class ResultsWriter:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def trials_path(self) -> Path:
        return self.directory / "trials.jsonl"

    def write_manifest(self, manifest: Mapping[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / "manifest.json"
        if path.exists():
            raise FileExistsError(f"{path} already exists; each run gets a new directory")
        path.write_text(_dumps(manifest) + "\n", encoding="utf-8")

    def append(self, kind: str, trial: Mapping[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.trials_path.open("a", encoding="utf-8") as handle:
            handle.write(_dumps({"kind": kind, **trial}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def finish(
        self,
        *,
        complete: bool,
        reason: str | None,
        spent_usd: Decimal,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        status = {"complete": complete, "reason": reason, "spent_usd": spent_usd, **(extra or {})}
        (self.directory / "status.json").write_text(_dumps(status) + "\n", encoding="utf-8")

    def finished(self) -> dict[tuple[str, str, str | None, int], dict[str, Any]]:
        """Trials that ran (anything but a budget stop), keyed by (kind, case, profile, repeat).
        A resumed run skips these."""
        return {
            (t["kind"], t["case_id"], t.get("profile"), int(t["repeat"])): t
            for t in self.trials()
            if t["status"] != STATUS_BUDGET_STOPPED
        }

    def drop_stopped(self) -> int:
        """Remove budget-stop placeholders before resuming; those trials will now run.
        Returns how many were removed."""
        if not self.trials_path.exists():
            return 0
        lines = self.trials_path.read_text(encoding="utf-8").splitlines(keepends=True)
        keep = [
            line
            for line in lines
            if line.strip() and json.loads(line)["status"] != STATUS_BUDGET_STOPPED
        ]
        temporary = self.trials_path.with_suffix(".tmp")
        temporary.write_text("".join(keep), encoding="utf-8")
        temporary.replace(self.trials_path)
        return sum(1 for line in lines if line.strip()) - len(keep)

    def add_session(self, session: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Record one start or resume of the run; returns every session so far."""
        path = self.directory / "sessions.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_dumps(session) + "\n")
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def trials(self, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.trials_path.exists():
            return []
        lines = self.trials_path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines if line.strip()]
        return [r for r in records if kind is None or r["kind"] == kind]


def run_trials[Case](
    cases: Sequence[Case],
    run_one: Callable[[Case], Mapping[str, Any]],
    *,
    writer: ResultsWriter,
    kind: str,
    stopped: Callable[[Case], Mapping[str, Any]],
) -> bool:
    """Run each case in order and write its record as soon as it finishes.

    If the budget refuses a call, that case and every later one are written as `stopped(case)`
    (which should carry `status: budget_stopped`), and False is returned. True means all ran.
    """
    for index, case in enumerate(cases):
        try:
            record = run_one(case)
        except BudgetExceededError:
            _write_stopped(cases[index:], writer=writer, kind=kind, stopped=stopped)
            return False
        writer.append(kind, record)
    return True


def _write_stopped[Case](
    cases: Iterable[Case],
    *,
    writer: ResultsWriter,
    kind: str,
    stopped: Callable[[Case], Mapping[str, Any]],
) -> None:
    for case in cases:
        writer.append(kind, stopped(case))
