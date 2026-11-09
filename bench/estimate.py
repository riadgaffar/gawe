"""Pricing a run before it starts (design section 4).

1. Free count: every request the run will send is counted exactly (bench/count.py). From that:
   * input cost: each request's first attempt, plus a 5% margin;
   * hard ceiling: every allowed attempt of every request using its whole `max_output_tokens`.
     Spend cannot go past this unless the price table is wrong.
2. Optional paid trial: three contracts (shortest, median, longest; both profiles) and one clean
   twin per app, read for real. It measures attempts and output per call, which gives an
   expected total.

An estimate is saved with a fingerprint of everything it depends on (model, price row, sample,
profiles, repeats, corpus), so `run --estimate-from` refuses an estimate made for another run.
"""

import json
import statistics
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from bench.budget import INPUT_MARGIN, CallRecord, Estimate, Price
from bench.count import TokenCounter, largest_findings, planner_request, reader_request
from bench.cuad.labels import CuadCase, read_text
from bench.cuad.profile import PROFILES
from bench.cuad.sample import sample_ids_sha256
from bench.injection.cases import Case, document_for
from bench.injection.run import BUNDLES
from gawe.core.registry import ToolRegistry

READ_ATTEMPTS = 2  # Reader's default max_attempts (a test keeps these in step)
PLAN_ATTEMPTS = 2  # Planner's default max_attempts


class EstimateMismatchError(ValueError):
    """A saved estimate was made for a different run."""


@dataclass(frozen=True)
class Unit:
    """One request a run will send, `repeats` times, with up to `attempts` tries each."""

    kind: str  # "cuad" or "injection"
    case_id: str
    profile: str | None
    purpose: str  # "read" or "plan"
    input_tokens: int
    max_output_tokens: int
    attempts: int
    repeats: int


@dataclass(frozen=True)
class RunPlan:
    cuad_cases: tuple[CuadCase, ...]
    profiles: tuple[str, ...]
    cuad_repeats: int
    injection_cases: tuple[Case, ...]
    injection_repeats: int

    def fingerprint(self, *, price: Price, corpus_sha256: str) -> dict[str, Any]:
        return {
            "price": price.as_manifest(),
            "cuad_sample_sha256": sample_ids_sha256(c.case_id for c in self.cuad_cases),
            "cuad_texts_sha256": sample_ids_sha256(c.text_sha256 for c in self.cuad_cases),
            "profiles": list(self.profiles),
            "cuad_repeats": self.cuad_repeats,
            "injection_cases": sorted(c.case_id for c in self.injection_cases),
            "injection_repeats": self.injection_repeats,
            "corpus_sha256": corpus_sha256,
        }


def count_units(plan: RunPlan, counter: TokenCounter, *, cuad_root: Path) -> list[Unit]:
    units: list[Unit] = []
    for case in plan.cuad_cases:
        text = read_text(cuad_root, case)
        for profile in plan.profiles:
            request = reader_request(PROFILES[profile], text)
            units.append(
                Unit(
                    kind="cuad",
                    case_id=case.case_id,
                    profile=profile,
                    purpose="read",
                    input_tokens=counter.count(request),
                    max_output_tokens=request.max_output_tokens,
                    attempts=READ_ATTEMPTS,
                    repeats=plan.cuad_repeats,
                )
            )
    with tempfile.TemporaryDirectory(prefix="gawe-bench-count-") as tmp:
        apps = {app: build(Path(tmp) / app) for app, build in BUNDLES.items()}
        for item in plan.injection_cases:
            bundle = apps[item.app]
            assert bundle.reader_profile is not None
            read = reader_request(bundle.reader_profile, document_for(item))
            registry = ToolRegistry()
            for manifest in bundle.manifests:
                registry.register(manifest)
            plan_request = planner_request(
                registry, item.objective, largest_findings(bundle.reader_profile)
            )
            for purpose, request, attempts in (
                ("read", read, READ_ATTEMPTS),
                ("plan", plan_request, PLAN_ATTEMPTS),
            ):
                units.append(
                    Unit(
                        kind="injection",
                        case_id=item.case_id,
                        profile=None,
                        purpose=purpose,
                        input_tokens=counter.count(request),
                        max_output_tokens=request.max_output_tokens,
                        attempts=attempts,
                        repeats=plan.injection_repeats,
                    )
                )
    return units


@dataclass(frozen=True)
class PurposeStats:
    units: int
    calls: int
    avg_attempts: float
    avg_output_tokens: float
    early_stops: int
    hit_output_cap: int


@dataclass(frozen=True)
class TrialStats:
    spent_usd: Decimal
    by_purpose: Mapping[str, PurposeStats]
    models: tuple[str, ...]


def trial_stats(
    history: Sequence[CallRecord], *, units: Mapping[str, int], spent_usd: Decimal
) -> TrialStats:
    """Average attempts and output per call, by purpose, from a trial's calls."""
    by_purpose: dict[str, PurposeStats] = {}
    for purpose, count in units.items():
        calls = [c for c in history if c.purpose == purpose]
        by_purpose[purpose] = PurposeStats(
            units=count,
            calls=len(calls),
            avg_attempts=len(calls) / count if count else 0.0,
            avg_output_tokens=statistics.fmean(c.output_tokens for c in calls) if calls else 0.0,
            early_stops=sum(1 for c in calls if c.early_stop),
            hit_output_cap=sum(1 for c in calls if c.hit_output_cap),
        )
    models = tuple(sorted({c.model for c in history if c.model}))
    return TrialStats(spent_usd=spent_usd, by_purpose=by_purpose, models=models)


def trial_contracts(cases: Sequence[CuadCase]) -> list[CuadCase]:
    """Shortest, median and longest contract of the sample (fewer if the sample is smaller)."""
    ordered = sorted(cases, key=lambda c: (c.chars, c.case_id))
    if not ordered:
        return []
    picks = [ordered[0], ordered[len(ordered) // 2], ordered[-1]]
    unique: list[CuadCase] = []
    for case in picks:
        if case not in unique:
            unique.append(case)
    return unique


def build_estimate(
    units: Iterable[Unit], price: Price, trial: TrialStats | None = None
) -> Estimate:
    input_usd = ceiling = Decimal(0)
    expected: Decimal | None = Decimal(0) if trial is not None else None
    for unit in units:
        tokens_in = Decimal(unit.input_tokens) * INPUT_MARGIN
        per_attempt_in = tokens_in * price.input_per_mtok / 1_000_000
        input_usd += unit.repeats * per_attempt_in
        worst_out = unit.max_output_tokens * price.output_per_mtok / 1_000_000
        ceiling += unit.repeats * unit.attempts * (per_attempt_in + worst_out)
        if trial is not None and expected is not None:
            stats = trial.by_purpose.get(unit.purpose)
            attempts = Decimal(str(stats.avg_attempts)) if stats and stats.calls else Decimal(1)
            output = (
                Decimal(str(stats.avg_output_tokens))
                if stats and stats.calls
                else Decimal(unit.max_output_tokens)
            )
            out_usd = output * price.output_per_mtok / 1_000_000
            expected += unit.repeats * attempts * (per_attempt_in + out_usd)
    if trial is not None and expected is not None:
        expected += trial.spent_usd  # the trial's own spend counts toward the cap
    return Estimate(input_usd=input_usd, ceiling_usd=ceiling, expected_usd=expected)


def save_estimate(
    path: Path,
    estimate: Estimate,
    *,
    fingerprint: Mapping[str, Any],
    units: Sequence[Unit],
    trial: TrialStats | None,
) -> None:
    body = {
        "schema": "gawe.bench.estimate.v1",
        "fingerprint": dict(fingerprint),
        "estimate": {
            "input_usd": str(estimate.input_usd),
            "ceiling_usd": str(estimate.ceiling_usd),
            "expected_usd": None if estimate.expected_usd is None else str(estimate.expected_usd),
        },
        "units": len(units),
        "input_tokens": sum(u.input_tokens * u.repeats for u in units),
        "trial": _trial_json(trial),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_estimate(path: Path, *, fingerprint: Mapping[str, Any]) -> Estimate:
    body = json.loads(path.read_text(encoding="utf-8"))
    if body.get("schema") != "gawe.bench.estimate.v1":
        raise EstimateMismatchError(f"{path} is not a saved benchmark estimate")
    saved = body.get("fingerprint", {})
    differs = sorted(k for k in set(saved) | set(fingerprint) if saved.get(k) != fingerprint.get(k))
    if differs:
        raise EstimateMismatchError(
            f"{path} was made for a different run (differs in: {', '.join(differs)})"
        )
    figures = body["estimate"]
    expected = figures.get("expected_usd")
    return Estimate(
        input_usd=Decimal(figures["input_usd"]),
        ceiling_usd=Decimal(figures["ceiling_usd"]),
        expected_usd=None if expected is None else Decimal(expected),
    )


def load_trial_stats(path: Path, *, fingerprint: Mapping[str, Any]) -> TrialStats | None:
    """A saved estimate's trial measurements, for pricing the rest of a resumed run. The trial's
    own spend is left out: it was paid before the run started."""
    load_estimate(path, fingerprint=fingerprint)  # same checks: it must be for this run
    trial = json.loads(path.read_text(encoding="utf-8")).get("trial")
    if not trial:
        return None
    return TrialStats(
        spent_usd=Decimal(0),
        by_purpose={k: PurposeStats(**v) for k, v in trial["by_purpose"].items()},
        models=tuple(trial["models"]),
    )


def remaining(units: Sequence[Unit], finished: Iterable[tuple[str, str, str | None]]) -> list[Unit]:
    """`units` with each one's repeats reduced by the trials of it already finished."""
    done: dict[tuple[str, str, str | None], int] = {}
    for key in finished:
        done[key] = done.get(key, 0) + 1
    left = []
    for unit in units:
        repeats = unit.repeats - done.get((unit.kind, unit.case_id, unit.profile), 0)
        if repeats > 0:
            left.append(replace(unit, repeats=repeats))
    return left


def _trial_json(trial: TrialStats | None) -> dict[str, Any] | None:
    if trial is None:
        return None
    return {
        "spent_usd": str(trial.spent_usd),
        "models": list(trial.models),
        "by_purpose": {k: asdict(v) for k, v in trial.by_purpose.items()},
    }
