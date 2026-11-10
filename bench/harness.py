"""Puts a benchmark run together: data, sample, estimate, the start decision, the runs, the report.

Order inside a run follows the design's priorities (section 5.3): the `presence` profile first,
then the injection suite, then the `production` profile. If the budget runs out, what is missing
is the second column, never the main number or the injection evidence.

Everything that touches the outside world (the data, the model, the token counter, git, the
clock, the confirmation prompt) comes in through `Deps`, so tests run the whole flow offline.
"""

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from bench.budget import (
    Budget,
    CountingProvider,
    Estimate,
    Price,
    estimate_input_tokens,
    start_decision,
)
from bench.count import AnthropicTokenCounter, TokenCounter
from bench.cuad import fetch as cuad_fetch
from bench.cuad.labels import CuadCase, CuadCorpus, load_corpus
from bench.cuad.mapping import partial_kinds, scored_kinds
from bench.cuad.profile import PROFILES, profile_sha256
from bench.cuad.run import plan_trials as plan_cuad
from bench.cuad.run import run_cuad
from bench.cuad.sample import Sample, stratified_sample
from bench.estimate import (
    RunPlan,
    Unit,
    build_estimate,
    count_units,
    load_estimate,
    load_trial_stats,
    remaining,
    save_estimate,
    trial_contracts,
    trial_stats,
)
from bench.injection.cases import Suite, corpus_sha256, load_suite
from bench.injection.run import plan_trials as plan_injection
from bench.injection.run import run_injection
from bench.injection.scripted import KIND as SCRIPTED_KIND
from bench.injection.scripted import MODES as SCRIPTED_MODES
from bench.injection.scripted import run_scripted, scripted_trials
from bench.metrics import prevalence
from bench.report import write_report
from bench.results import RESULTS_DIR, ResultsWriter
from gawe.llm.provider import LLMProvider, LLMRequest, ProviderConfigError
from gawe.llm.reader import DEFAULT_MAX_DOCUMENT_CHARS

DEFAULT_SEED = 20261029


@dataclass(frozen=True)
class Settings:
    cap: Decimal
    model: str
    price: Price
    seed: int = DEFAULT_SEED
    contracts: int = 50
    min_positives: int = 10
    cuad_repeats: int = 3
    injection_repeats: int = 5
    profiles: tuple[str, ...] = ("presence", "production")


def _git() -> tuple[str, bool]:
    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
        return done.stdout.strip()

    try:
        return git("rev-parse", "HEAD"), bool(git("status", "--porcelain"))
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True


def _provider(model: str) -> LLMProvider:
    from gawe.llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider(model=model)


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


@dataclass
class Deps:
    data_root: Callable[[], Path] = cuad_fetch.fetch
    archive_sha256: Callable[[], str | None] = lambda: cuad_fetch.PINNED_SHA256
    counter: Callable[[str], TokenCounter] = lambda model: AnthropicTokenCounter(model)
    provider: Callable[[str], LLMProvider] = _provider
    results_dir: Path = RESULTS_DIR
    confirm: Callable[[str], bool] = _confirm
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    git: Callable[[], tuple[str, bool]] = _git
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Prepared:
    root: Path
    corpus: CuadCorpus
    sample: Sample
    suite: Suite
    plan: RunPlan
    fingerprint: Mapping[str, Any]


def prepare(settings: Settings, deps: Deps) -> Prepared:
    root = deps.data_root()
    corpus = load_corpus(root, max_chars=DEFAULT_MAX_DOCUMENT_CHARS)
    sample = stratified_sample(
        corpus.cases,
        seed=settings.seed,
        size=settings.contracts,
        min_positives=settings.min_positives,
    )
    suite = load_suite()
    plan = RunPlan(
        cuad_cases=sample.cases,
        profiles=settings.profiles,
        cuad_repeats=settings.cuad_repeats,
        injection_cases=suite.cases,
        injection_repeats=settings.injection_repeats,
    )
    fingerprint = {
        "model": settings.model,
        **plan.fingerprint(price=settings.price, corpus_sha256=corpus_sha256()),
    }
    return Prepared(root, corpus, sample, suite, plan, fingerprint)


def _stamp(deps: Deps) -> str:
    return deps.now().strftime("%Y%m%dT%H%M%SZ")


def request_key(request: LLMRequest) -> str:
    return hashlib.sha256((request.system + "\0" + request.user).encode()).hexdigest()


class ExactCounts:
    """Per-call input estimate: the free count's exact figure when the request was counted."""

    def __init__(self, counts: Mapping[str, int]) -> None:
        self._counts = dict(counts)

    def __call__(self, request: LLMRequest) -> int:
        return self._counts.get(request_key(request), estimate_input_tokens(request))


class _RecordingCounter:
    def __init__(self, inner: TokenCounter) -> None:
        self._inner = inner
        self.counts: dict[str, int] = {}

    def count(self, request: LLMRequest) -> int:
        tokens = self._inner.count(request)
        self.counts[request_key(request)] = tokens
        return tokens


def free_count(
    prepared: Prepared, settings: Settings, deps: Deps
) -> tuple[list[Unit], ExactCounts]:
    counter = _RecordingCounter(deps.counter(settings.model))
    units = count_units(prepared.plan, counter, cuad_root=prepared.root)
    return units, ExactCounts(counter.counts)


def run_estimate(
    settings: Settings, deps: Deps, *, trial: bool, out: TextIO
) -> tuple[Estimate, Path]:
    """The free count, plus a paid trial if asked; saves and prints the estimate."""
    prepared = prepare(settings, deps)
    units, exact = free_count(prepared, settings, deps)
    stats = None
    if trial:
        budget = Budget(cap=settings.cap, price=settings.price)
        provider = CountingProvider(deps.provider(settings.model), budget, input_tokens=exact)
        writer = ResultsWriter(deps.results_dir / f"trial-{_stamp(deps)}")
        contracts = trial_contracts(prepared.sample.cases)
        run_cuad(
            prepared.root,
            plan_cuad(contracts, settings.profiles, 1),
            provider=provider,
            profiles=PROFILES,
            writer=writer,
            sleep=deps.sleep,
        )
        twins = [next(t for t in prepared.suite.twins if t.app == app) for app in _apps(prepared)]
        run_injection(plan_injection(twins, 1), provider=provider, writer=writer, sleep=deps.sleep)
        stats = trial_stats(
            provider.history,
            units={
                "read": len(contracts) * len(settings.profiles) + len(twins),
                "plan": len(twins),
            },
            spent_usd=budget.spent,
        )
    estimate = build_estimate(units, settings.price, stats)
    path = deps.results_dir / "estimates" / f"estimate-{_stamp(deps)}.json"
    save_estimate(path, estimate, fingerprint=prepared.fingerprint, units=units, trial=stats)
    _print_estimate(estimate, settings, prepared, units, stats is not None, out)
    if stats is not None:
        read = stats.by_purpose["read"]
        print(
            f"trial: spent ${stats.spent_usd:.4f}; reads averaged {read.avg_attempts:.2f} attempts "
            f"and {read.avg_output_tokens:.0f} output tokens; {read.hit_output_cap} of "
            f"{read.calls} reads hit the output cap ({read.early_stops} stopped early)",
            file=out,
        )
    print(f"estimate saved to {path}", file=out)
    return estimate, path


def _apps(prepared: Prepared) -> list[str]:
    return sorted({c.app for c in prepared.suite.twins})


def _print_estimate(
    estimate: Estimate,
    settings: Settings,
    prepared: Prepared,
    units: list[Unit],
    trial: bool,
    out: TextIO,
) -> None:
    calls = sum(u.repeats for u in units)
    print(
        f"{len(prepared.sample.cases)} contracts x {len(settings.profiles)} profiles x "
        f"{settings.cuad_repeats} repeats, and {len(prepared.suite.cases)} injection cases x "
        f"{settings.injection_repeats} repeats: {calls} requests before retries",
        file=out,
    )
    print(f"input cost (exact count + 5%): ${estimate.input_usd:.4f}", file=out)
    print(f"hard ceiling (every attempt, full output): ${estimate.ceiling_usd:.4f}", file=out)
    if trial and estimate.expected_usd is not None:
        print(f"expected total (from the trial): ${estimate.expected_usd:.4f}", file=out)


class ResumeMismatchError(ValueError):
    """The run being resumed was set up differently from the settings given now."""


def run_benchmark(
    settings: Settings,
    deps: Deps,
    *,
    yes: bool,
    accept_ceiling_over_cap: bool,
    estimate_from: Path | None,
    out: TextIO,
    err: TextIO,
    resume: Path | None = None,
) -> int:
    """Returns a CLI exit code: 0 complete, 1 stopped (spend cap or API refusal), 2 refused.

    With `resume`, an earlier run continues in its own directory: trials it finished are kept and
    skipped, budget-stop placeholders are dropped, and `settings.cap` caps this session's new spend.
    """
    prepared = prepare(settings, deps)
    finished: dict[tuple[str, str, str | None, int], dict[str, Any]] = {}
    if resume is not None:
        writer = ResultsWriter(resume)
        _check_resumable(writer, prepared, settings, deps)
        finished = writer.finished()

    if estimate_from is not None and resume is None:
        estimate = load_estimate(estimate_from, fingerprint=prepared.fingerprint)
        exact = ExactCounts({})
    else:
        units, exact = free_count(prepared, settings, deps)
        trial = (
            load_trial_stats(estimate_from, fingerprint=prepared.fingerprint)
            if estimate_from is not None
            else None
        )
        units = remaining(units, (k[:3] for k in finished))
        estimate = build_estimate(units, settings.price, trial)
        if resume is not None:
            print(f"resuming: {len(finished)} trials already finished", file=out)
        _print_estimate(estimate, settings, prepared, units, trial is not None, out)
    decision = start_decision(
        estimate, settings.cap, accept_ceiling_over_cap=accept_ceiling_over_cap
    )
    if not decision.ok:
        print(f"refused: {decision.reason}", file=err)
        return 2
    print(f"ok to start: {decision.reason}", file=out)
    verb = "Resume" if resume is not None else "Start"
    if not yes and not deps.confirm(f"{verb} the benchmark under a ${settings.cap} cap? [y/N] "):
        print("not started; nothing was spent", file=err)
        return 2

    if resume is None:
        run_id = _stamp(deps)
        writer = ResultsWriter(deps.results_dir / run_id)
        writer.write_manifest(build_manifest(run_id, settings, prepared, deps))
    else:
        writer.drop_stopped()
        _record_lost_session(writer)
    started = deps.now()
    budget = Budget(cap=settings.cap, price=settings.price)
    provider = CountingProvider(deps.provider(settings.model), budget, input_tokens=exact)

    def todo(
        trials: list[Any], key: Callable[[Any], tuple[str, str, str | None, int]]
    ) -> list[Any]:
        return [t for t in trials if key(t) not in finished]

    complete, reason = True, None
    try:
        if "presence" in settings.profiles:
            complete = run_cuad(
                prepared.root,
                todo(
                    plan_cuad(prepared.sample.cases, ("presence",), settings.cuad_repeats),
                    _cuad_key,
                ),
                provider=provider,
                profiles=PROFILES,
                writer=writer,
                sleep=deps.sleep,
            )
        if complete:
            complete = run_injection(
                todo(
                    plan_injection(prepared.suite.cases, settings.injection_repeats), _injection_key
                ),
                provider=provider,
                writer=writer,
                sleep=deps.sleep,
            )
        if complete and "production" in settings.profiles:
            complete = run_cuad(
                prepared.root,
                todo(
                    plan_cuad(prepared.sample.cases, ("production",), settings.cuad_repeats),
                    _cuad_key,
                ),
                provider=provider,
                profiles=PROFILES,
                writer=writer,
                sleep=deps.sleep,
            )
        if not complete:
            reason = "the spend cap was reached"
    except ProviderConfigError as exc:
        complete = False
        reason = f"the Anthropic API refused a request ({str(exc)[:240]})"
        print(f"stopped: {reason}", file=err)

    commit, dirty = deps.git()
    sessions = writer.add_session(
        {
            "started_at": started,
            "ended_at": deps.now(),
            "git_commit": commit,
            "git_dirty": dirty,
            "cap_usd": settings.cap,
            "spent_usd": budget.spent,
            "calls": budget.calls,
            "models_reported": sorted({c.model for c in provider.history if c.model}),
            "stopped": reason,
        }
    )
    total = sum((Decimal(str(x["spent_usd"])) for x in sessions), Decimal(0))
    trials = writer.trials()
    writer.finish(
        complete=complete,
        reason=reason,
        spent_usd=total,
        extra={
            "sessions": len(sessions),
            "calls": sum(int(t.get("calls", 0)) for t in trials),
            "input_tokens": sum(int(t.get("input_tokens", 0)) for t in trials),
            "output_tokens": sum(int(t.get("output_tokens", 0)) for t in trials),
            "early_stops": sum(int(t.get("early_stops", 0)) for t in trials),
            "hit_output_cap": sum(1 for t in trials if t.get("hit_output_cap")),
            "models_reported": sorted({m for x in sessions for m in x["models_reported"]}),
        },
    )
    _, md = write_report(writer.directory)
    print(
        f"this session spent ${budget.spent:.4f} of ${settings.cap}; whole run ${total:.4f}; "
        f"report: {md}",
        file=out,
    )
    return 0 if complete else 1


def _cuad_key(trial: Any) -> tuple[str, str, str | None, int]:
    return ("cuad", trial.case.case_id, trial.profile, trial.repeat)


def _injection_key(trial: Any) -> tuple[str, str, str | None, int]:
    return ("injection", trial.case.case_id, None, trial.repeat)


_RESUME_CHECKS = (
    ("model", lambda m: m["model"]),
    ("sample", lambda m: m["sample"]["ids_sha256"]),
    ("profiles", lambda m: list(m["profiles"])),
    ("repeats", lambda m: dict(m["repeats"])),
    ("attack corpus", lambda m: m["injection"]["corpus_sha256"]),
    ("prices", lambda m: dict(m["price"])),
)


def _check_resumable(
    writer: ResultsWriter, prepared: Prepared, settings: Settings, deps: Deps
) -> None:
    manifest_path = writer.directory / "manifest.json"
    if not manifest_path.is_file():
        raise ResumeMismatchError(f"{writer.directory} is not a benchmark run directory")
    status_path = writer.directory / "status.json"
    if status_path.is_file() and json.loads(status_path.read_text())["complete"]:
        raise ResumeMismatchError(f"{writer.directory} already finished; nothing to resume")
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    now = json.loads(json.dumps(build_manifest("resume", settings, prepared, deps), default=str))
    differs = [name for name, get in _RESUME_CHECKS if get(saved) != get(now)]
    if differs:
        raise ResumeMismatchError(
            f"{writer.directory} was run with different settings ({', '.join(differs)}); "
            "resume with the options it was started with"
        )


def _record_lost_session(writer: ResultsWriter) -> None:
    """A session that stopped without recording itself (the process ended abruptly) is added from
    its trial records, so the run's total spend still includes it. Only the first session can be
    lost this way (later ones find sessions.jsonl), so its commit and cap are the manifest's."""
    if (writer.directory / "sessions.jsonl").exists():
        return
    trials = writer.trials()
    if not trials:
        return
    manifest = json.loads((writer.directory / "manifest.json").read_text(encoding="utf-8"))
    writer.add_session(
        {
            "started_at": manifest["started_at"],
            "ended_at": None,
            "git_commit": manifest["git_commit"],
            "git_dirty": manifest["git_dirty"],
            "cap_usd": manifest["max_usd"],
            "spent_usd": sum((Decimal(str(t.get("cost_usd", "0"))) for t in trials), Decimal(0)),
            "calls": sum(int(t.get("calls", 0)) for t in trials),
            "models_reported": [],
            "stopped": "ended without recording itself; spend summed from its trial records",
        }
    )


def build_manifest(
    run_id: str, settings: Settings, prepared: Prepared, deps: Deps
) -> dict[str, Any]:
    commit, dirty = deps.git()
    kinds = (*scored_kinds(), *partial_kinds())
    sample, corpus = prepared.sample, prepared.corpus
    return {
        "schema": "gawe.bench.manifest.v1",
        "bench_run_id": run_id,
        "started_at": deps.now().isoformat(),
        "git_commit": commit,
        "git_dirty": dirty,
        "model": settings.model,
        "provider_settings": {
            "temperature": "API default",
            "reader_max_output_tokens": 2048,
            "planner_max_output_tokens": 4096,
        },
        "price": settings.price.as_manifest(),
        "max_usd": settings.cap,
        "repeats": {"cuad": settings.cuad_repeats, "injection": settings.injection_repeats},
        "profiles": list(settings.profiles),
        "profile_sha256": {name: profile_sha256(p) for name, p in PROFILES.items()},
        "dataset": {
            "archive_sha256": deps.archive_sha256(),
            "contracts_usable": len(corpus.cases),
            "too_large": [case_id for case_id, _ in corpus.too_large],
            "missing_text": list(corpus.missing_text),
            "prevalence": {k: prevalence(corpus.cases, k) for k in kinds},
        },
        "sample": {
            "seed": sample.seed,
            "size_requested": sample.size_requested,
            "ids_sha256": sample.ids_sha256,
            "topped_up": sample.topped_up,
            "shortfall": sample.shortfall,
            "cases": [_case_json(c) for c in sample.cases],
        },
        "injection": {
            "attacks": len(prepared.suite.attacks),
            "twins": len(prepared.suite.twins),
            "corpus_sha256": corpus_sha256(),
            "policy_by_goal": {name: g.policy for name, g in prepared.suite.goals.items()},
        },
    }


def run_scripted_suite(deps: Deps, out: TextIO) -> Path:
    """`python -m bench scripted`: every attack with a model fooled on purpose. No model is
    called and nothing is spent; `deps.provider` is never touched."""
    suite = load_suite()
    trials = scripted_trials(suite)
    run_id = _stamp(deps) + "-scripted"
    commit, dirty = deps.git()
    writer = ResultsWriter(deps.results_dir / run_id)
    writer.write_manifest(
        {
            "schema": "gawe.bench.scripted.v1",
            "kind": "scripted",
            "bench_run_id": run_id,
            "started_at": deps.now().isoformat(),
            "git_commit": commit,
            "git_dirty": dirty,
            "model": None,
            "modes": list(SCRIPTED_MODES),
            "injection": {
                "attacks": len(suite.attacks),
                "trials": len(trials),
                "corpus_sha256": corpus_sha256(),
                "policy_by_goal": {name: g.policy for name, g in suite.goals.items()},
            },
        }
    )
    print(
        f"scripted: {len(trials)} runs ({len(suite.attacks)} attacks, modes "
        f"{', '.join(SCRIPTED_MODES)}); no model is called",
        file=out,
    )
    run_scripted(trials, writer=writer)
    writer.finish(complete=True, reason=None, spent_usd=Decimal(0))
    print(f"wrote {len(writer.trials(SCRIPTED_KIND))} records to {writer.directory}", file=out)
    return writer.directory


def _case_json(case: CuadCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "contract_type": case.contract_type,
        "text_sha256": case.text_sha256,
        "chars": case.chars,
        "labels": dict(case.labels),
        "partial_labels": dict(case.partial_labels),
    }
