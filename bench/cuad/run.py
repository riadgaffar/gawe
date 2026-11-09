"""Runs the Reader over sampled CUAD contracts and writes one record per read.

Trials run `presence` first, every repeat, then `production`: if the budget runs out, the main
number is complete and the second column is what is missing (design section 5.3).

Each read goes through the shipped Reader (same sanitizing, fence, schema and retries) on a
metered provider. A record keeps the flagged kinds, token counts and cost; never the contract
text or the model's answer.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from bench.budget import CountingProvider
from bench.cuad.labels import CuadCase, read_text
from bench.results import STATUS_BUDGET_STOPPED, ResultsWriter, run_trials
from gawe.llm.provider import LLMUnavailableError
from gawe.llm.reader import DocumentTooLargeError, Reader, ReaderError, ReaderProfile

KIND = "cuad"
PROFILE_ORDER = ("presence", "production")
UNAVAILABLE_RETRIES = 2


@dataclass(frozen=True)
class Trial:
    case: CuadCase
    profile: str
    repeat: int


def plan_trials(cases: Sequence[CuadCase], profiles: Sequence[str], repeats: int) -> list[Trial]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    unknown = set(profiles) - set(PROFILE_ORDER)
    if unknown:
        raise ValueError(f"unknown profiles: {sorted(unknown)}")
    ordered = [p for p in PROFILE_ORDER if p in profiles]
    return [
        Trial(case, profile, repeat)
        for profile in ordered
        for repeat in range(1, repeats + 1)
        for case in cases
    ]


def run_cuad(
    root: Path,
    trials: Sequence[Trial],
    *,
    provider: CountingProvider,
    profiles: dict[str, ReaderProfile],
    writer: ResultsWriter,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Run every trial; False if the budget stopped the run early."""
    readers = {name: Reader(provider, profiles[name]) for name in {t.profile for t in trials}}

    def run_one(trial: Trial) -> dict[str, Any]:
        provider.take_records()  # attribute only this trial's calls to it
        started = clock()
        text = read_text(root, trial.case)
        status, flagged, attempts = _read(readers[trial.profile], text, sleep)
        calls = provider.take_records()
        return {
            **_ids(trial),
            "status": status,
            "flagged": flagged,
            "attempts": attempts,
            "calls": len(calls),
            "input_tokens": sum(c.input_tokens for c in calls),
            "output_tokens": sum(c.output_tokens for c in calls),
            "cost_usd": sum((c.cost_usd for c in calls), Decimal(0)),
            "hit_output_cap": any(c.hit_output_cap for c in calls),
            "early_stops": sum(1 for c in calls if c.early_stop),
            "seconds": round(clock() - started, 3),
        }

    def stopped(trial: Trial) -> dict[str, Any]:
        return {**_ids(trial), "status": STATUS_BUDGET_STOPPED, "flagged": []}

    return run_trials(trials, run_one, writer=writer, kind=KIND, stopped=stopped)


def _ids(trial: Trial) -> dict[str, Any]:
    return {"case_id": trial.case.case_id, "profile": trial.profile, "repeat": trial.repeat}


def _read(reader: Reader, text: str, sleep: Callable[[float], None]) -> tuple[str, list[str], int]:
    for attempt in range(UNAVAILABLE_RETRIES + 1):
        try:
            result = reader.read(text)
        except LLMUnavailableError:
            if attempt == UNAVAILABLE_RETRIES:
                return "unavailable", [], 0
            sleep(2.0 * 2**attempt)
            continue
        except DocumentTooLargeError:
            return "too_large", [], 0
        except ReaderError:
            return "reader_error", [], 0
        clauses = result.findings.data.get("flagged_clauses", [])
        flagged = sorted({str(c["kind"]) for c in clauses})
        return "ok", flagged, result.attempts
    raise AssertionError("unreachable")
