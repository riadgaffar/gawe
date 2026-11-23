"""Runs the Reader over sampled CUAD contracts and writes one record per read.

Trials run `presence` first, every repeat, then `production`: if the budget runs out, the main
number is complete and the second column is what is missing (design section 5.3).

Each read goes through the shipped Reader (same sanitizing, fence, schema and retries) on a
metered provider. A record keeps the flagged kinds, each flag's severity, the risk level, token
counts and cost; never the contract text, the summary or the parties. A rejected read also keeps
`reader_failures` (M19): one text-free label per rejected reply, such as `summary:maxLength`.
Severity and risk level
(since M17) are what the approval rules read, so approval volume can be worked out offline.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from bench.budget import CountingProvider
from bench.cuad.labels import CuadCase, read_text
from bench.injection.run import reader_failures
from bench.results import STATUS_BUDGET_STOPPED, ResultsWriter, run_trials
from gawe.llm.provider import LLMProvider, LLMRequest, LLMResponse, LLMUnavailableError
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
    replies = _Replies(provider)
    readers = {name: Reader(replies, profiles[name]) for name in {t.profile for t in trials}}

    def run_one(trial: Trial) -> dict[str, Any]:
        provider.take_records()  # attribute only this trial's calls to it
        replies.texts.clear()
        started = clock()
        text = read_text(root, trial.case)
        status, flags, risk_level, attempts = _read(readers[trial.profile], text, sleep)
        calls = provider.take_records()
        labels = (
            {"reader_failures": reader_failures(replies.texts, profiles[trial.profile].schema)}
            if status == "reader_error"
            else {}
        )
        replies.texts.clear()  # model replies are kept for this trial only, never recorded
        return {
            **labels,
            **_ids(trial),
            "status": status,
            "flagged": sorted({f["kind"] for f in flags}),
            "flags": flags,
            "risk_level": risk_level,
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
        return {
            **_ids(trial),
            "status": STATUS_BUDGET_STOPPED,
            "flagged": [],
            "flags": [],
            "risk_level": None,
        }

    return run_trials(trials, run_one, writer=writer, kind=KIND, stopped=stopped)


class _Replies:
    """Keeps the text of each reply for the current trial only, so a rejected read can say why
    (M19, as the injection harness has since M18). Never written to a record."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.texts: list[str] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        response = self._inner.complete(request)
        self.texts.append(response.text)
        return response


def _ids(trial: Trial) -> dict[str, Any]:
    return {"case_id": trial.case.case_id, "profile": trial.profile, "repeat": trial.repeat}


Flag = dict[str, str]  # {"kind": ..., "severity": ...}


def _read(
    reader: Reader, text: str, sleep: Callable[[float], None]
) -> tuple[str, list[Flag], str | None, int]:
    for attempt in range(UNAVAILABLE_RETRIES + 1):
        try:
            result = reader.read(text)
        except LLMUnavailableError:
            if attempt == UNAVAILABLE_RETRIES:
                return "unavailable", [], None, 0
            sleep(2.0 * 2**attempt)
            continue
        except DocumentTooLargeError:
            return "too_large", [], None, 0
        except ReaderError:
            return "reader_error", [], None, 0
        data = result.findings.data
        return "ok", flags_of(data.get("flagged_clauses", [])), _risk(data), result.attempts
    raise AssertionError("unreachable")


SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}


def flags_of(clauses: Sequence[Mapping[str, Any]]) -> list[Flag]:
    """Each distinct (kind, severity) pair, sorted. The schema bounds both to enumerations."""
    pairs = {(str(c["kind"]), str(c["severity"])) for c in clauses}
    ordered = sorted(pairs, key=lambda p: (p[0], SEVERITY_RANK.get(p[1], 9)))
    return [{"kind": kind, "severity": severity} for kind, severity in ordered]


def _risk(data: Mapping[str, Any]) -> str | None:
    value = data.get("risk_level")
    return str(value) if value is not None else None
