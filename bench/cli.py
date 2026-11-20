"""Command line for the benchmark.

    python -m bench data
    python -m bench estimate --max-usd 2 [--trial]
    python -m bench run --max-usd 25 [--yes] [--accept-ceiling-over-cap] [--estimate-from FILE]
    python -m bench run --max-usd 15 --resume bench-results/<run id> [--estimate-from FILE]
    python -m bench run --max-usd 16 --suites cuad --exclude-run bench-results/<run id> ...
    python -m bench approvals bench-results/<run id>
    python -m bench report bench-results/<run id>
    python -m bench scripted
    python -m bench integrity

`scripted` runs every attack with a model fooled on purpose (M13): no model is called, nothing is
spent, so it takes no cap and no model. It writes a run directory and its report.

`integrity` runs the execution-integrity scenarios (M16a): poisoned tool output used as an email
recipient, and encoded text riding in the Reader's summary into an email body. They are separate
from the scripted suite's 38 runs and have their own expected records. No model is called; exit
code 1 means a scenario differed from its expected record.

Every command that may call a real model needs --max-usd: there is no default and no
environment fallback, so a cap is always a deliberate choice. The model comes from --model or
GAWE_LLM_MODEL and must have a row in bench/prices.yaml.

`estimate` without --trial spends nothing: it uses the free token-counting endpoint. With
--trial, --max-usd caps the trial's spend.

`--suites cuad` reads CUAD contracts only, without the injection suite. `--exclude-run DIR`
leaves out every contract that run sampled, so a second sample is disjoint from the first; with
`--min-positives 0` it keeps CUAD's own share of each clause type, which a volume estimate needs.

`liability` (M17) scores `unlimited_liability` by what CUAD says about the cap, against the D1
bars in docs/m17-plan.md; `--baseline` shows an earlier run on the same contracts. Free.

`scan-gate` (M17) runs the liability-language scan on every eligible CUAD contract and scores
its express level against CUAD's "Uncapped Liability" label, against the D2 bars. Free.

`approvals` (M17) works out, from a run's production reads, how many honest contracts each
approval rule would send to a person. It reads records only; nothing is called or spent.

A run stopped by the cap or by the API (for example, no credit left) keeps every finished trial;
`--resume` continues it without paying for those again.

Exit codes: 0 done, 1 run stopped by the spend cap or the API (results so far are kept and
reported), 2 refused (bad arguments, no cap, unknown model, no data, over the cap, not confirmed).
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from bench.approvals import Scan, ScanHit, write_approvals
from bench.budget import Price, UnknownModelError, parse_usd, price_for
from bench.cuad.fetch import FetchError, fetch
from bench.cuad.labels import CuadFormatError, load_corpus
from bench.cuad.run import PROFILE_ORDER
from bench.estimate import EstimateMismatchError
from bench.harness import (
    DEFAULT_SEED,
    SUITES,
    Deps,
    ExcludeRunError,
    ResumeMismatchError,
    Settings,
    excluded_ids,
    run_benchmark,
    run_estimate,
    run_scripted_suite,
)
from bench.integrity.scenarios import run_suite
from bench.liability import cap_groups, write_liability
from bench.report import write_report
from bench.results import ResultsWriter
from bench.scan_gate import scan_cases, summarize_gate, write_gate
from gawe.llm.provider import ProviderConfigError

EXIT_OK, EXIT_STOPPED, EXIT_REFUSED = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bench", description="GAWE benchmark harness")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("data", help="download CUAD, check its pinned hash, extract it")

    estimate = commands.add_parser("estimate", help="price a run before spending on it")
    _common(estimate)
    estimate.add_argument(
        "--trial", action="store_true", help="also read three contracts for real (costs money)"
    )

    run = commands.add_parser("run", help="run the benchmark")
    _common(run)
    run.add_argument("--yes", action="store_true", help="start without asking for confirmation")
    run.add_argument(
        "--accept-ceiling-over-cap",
        action="store_true",
        help="start even if the worst case is over the cap, relying on the per-call stop",
    )
    run.add_argument("--estimate-from", metavar="FILE", help="reuse a saved estimate")
    run.add_argument(
        "--resume",
        metavar="DIR",
        help="continue a stopped run in its directory; --max-usd then caps the new spend only",
    )

    commands.add_parser(
        "scripted", help="run every attack with a model fooled on purpose (free, no model)"
    )

    commands.add_parser("integrity", help="run the execution-integrity scenarios (free, no model)")

    report = commands.add_parser("report", help="write report.json and report.md for a run")
    report.add_argument("directory", help="a run directory under bench-results/")

    approvals = commands.add_parser(
        "approvals", help="approval volume each rule implies for a run's production reads (free)"
    )
    approvals.add_argument("directory", help="a run directory under bench-results/")
    approvals.add_argument(
        "--no-express-rule",
        action="store_true",
        help="the express level failed its gate: every scan hit goes to next-day review",
    )

    commands.add_parser(
        "scan-gate", help="the liability scan on every eligible CUAD contract, vs its label (free)"
    )

    liability = commands.add_parser(
        "liability", help="score unlimited_liability by CUAD cap group against the D1 bars (free)"
    )
    liability.add_argument("directory", help="a run directory under bench-results/")
    liability.add_argument("--baseline", metavar="DIR", help="an earlier run to show alongside")
    return parser


def _common(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--max-usd", required=True, metavar="DOLLARS", help="spend cap for this command (required)"
    )
    command.add_argument("--model", help="model to benchmark (default: GAWE_LLM_MODEL)")
    command.add_argument("--seed", type=int, default=DEFAULT_SEED, help="sample seed")
    command.add_argument("--contracts", type=_positive_int, default=50, help="CUAD sample size")
    command.add_argument(
        "--min-positives", type=int, default=10, help="contracts per scored clause type"
    )
    command.add_argument("--cuad-repeats", type=_positive_int, default=3)
    command.add_argument("--injection-repeats", type=_positive_int, default=5)
    command.add_argument(
        "--profiles",
        default=",".join(PROFILE_ORDER),
        help="Reader profiles to score, comma-separated (default: presence,production)",
    )
    command.add_argument(
        "--suites",
        default=",".join(SUITES),
        help="suites to run, comma-separated (default: cuad,injection)",
    )
    command.add_argument(
        "--exclude-run",
        metavar="DIR",
        help="leave out every contract this earlier run sampled",
    )


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    deps: Deps | None = None,
) -> int:
    env = os.environ if env is None else env
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse has already printed why
        return EXIT_REFUSED if exc.code else EXIT_OK
    if args.command == "data":
        return _data(out, err)
    if args.command == "report":
        return _report(Path(args.directory), out, err)
    if args.command == "approvals":
        return _approvals(
            Path(args.directory), deps or Deps(), out, err, express_rule=not args.no_express_rule
        )
    if args.command == "scan-gate":
        return _scan_gate(deps or Deps(), out, err)
    if args.command == "liability":
        return _liability(args, deps or Deps(), out, err)
    if args.command == "scripted":
        directory = run_scripted_suite(deps or Deps(), out)
        return _report(directory, out, err)
    if args.command == "integrity":
        deps = deps or Deps()
        ok = run_suite(
            deps.results_dir, stamp=deps.now().strftime("%Y%m%dT%H%M%SZ"), git=deps.git(), out=out
        )
        return EXIT_OK if ok else EXIT_STOPPED
    settings = _settings(args, env, err)
    if settings is None:
        return EXIT_REFUSED
    _describe(args.command, settings.cap, settings.price, out)
    deps = deps or Deps()
    try:
        if args.command == "estimate":
            run_estimate(settings, deps, trial=args.trial, out=out)
            return EXIT_OK
        code = run_benchmark(
            settings,
            deps,
            yes=args.yes,
            accept_ceiling_over_cap=args.accept_ceiling_over_cap,
            estimate_from=Path(args.estimate_from) if args.estimate_from else None,
            out=out,
            err=err,
            resume=Path(args.resume) if args.resume else None,
        )
    except (
        FetchError,
        CuadFormatError,
        EstimateMismatchError,
        ResumeMismatchError,
        ProviderConfigError,
    ) as exc:
        print(f"refused: {exc}", file=err)
        return EXIT_REFUSED
    return {0: EXIT_OK, 1: EXIT_STOPPED}.get(code, EXIT_REFUSED)


def _settings(args: argparse.Namespace, env: Mapping[str, str], err: TextIO) -> Settings | None:
    try:
        cap = parse_usd(args.max_usd)
    except ValueError as exc:
        print(f"refused: {exc}", file=err)
        return None
    model = (args.model or env.get("GAWE_LLM_MODEL", "")).strip()
    if not model:
        print("refused: choose a model with --model or GAWE_LLM_MODEL", file=err)
        return None
    try:
        price = price_for(model)
    except UnknownModelError as exc:
        print(f"refused: {exc}", file=err)
        return None
    profiles = tuple(p.strip() for p in args.profiles.split(",") if p.strip())
    if not profiles or set(profiles) - set(PROFILE_ORDER):
        print(f"refused: --profiles must be some of {', '.join(PROFILE_ORDER)}", file=err)
        return None
    suites = tuple(x for x in SUITES if x in {p.strip() for p in args.suites.split(",")})
    if not suites or {p.strip() for p in args.suites.split(",") if p.strip()} - set(SUITES):
        print(f"refused: --suites must be some of {', '.join(SUITES)}", file=err)
        return None
    exclude: frozenset[str] = frozenset()
    if args.exclude_run:
        try:
            exclude = excluded_ids(Path(args.exclude_run))
        except ExcludeRunError as exc:
            print(f"refused: {exc}", file=err)
            return None
    return Settings(
        cap=cap,
        model=model,
        price=price,
        seed=args.seed,
        contracts=args.contracts,
        min_positives=args.min_positives,
        cuad_repeats=args.cuad_repeats,
        injection_repeats=args.injection_repeats,
        profiles=profiles,
        suites=suites,
        exclude=exclude,
        exclude_run=args.exclude_run,
    )


def _report(directory: Path, out: TextIO, err: TextIO) -> int:
    if not (directory / "manifest.json").is_file():
        print(f"refused: {directory} is not a benchmark run directory", file=err)
        return EXIT_REFUSED
    json_path, md_path = write_report(directory)
    print(f"wrote {json_path} and {md_path}", file=out)
    return EXIT_OK


def _approvals(
    directory: Path, deps: Deps, out: TextIO, err: TextIO, *, express_rule: bool = True
) -> int:
    """With CUAD at hand, the scan rows come from scanning each read contract; without it they
    are left out and the report says so."""
    if not (directory / "manifest.json").is_file():
        print(f"refused: {directory} is not a benchmark run directory", file=err)
        return EXIT_REFUSED
    scan: Scan | None = None
    try:
        root = deps.data_root()
        corpus = load_corpus(root)
    except (FetchError, CuadFormatError, OSError) as exc:
        print(f"note: CUAD unavailable, so no liability scan ({exc})", file=err)
    else:
        ids = {t["case_id"] for t in ResultsWriter(directory).trials("cuad")}
        cases = [c for c in corpus.cases if c.case_id in ids]
        if len(cases) == len(ids):
            hits = {
                case_id: ScanHit(express=found.express, carve_out=found.carve_out)
                for case_id, found in scan_cases(root, cases).items()
            }
            scan = hits.__getitem__
        else:
            print(f"note: {len(ids) - len(cases)} read contracts not in CUAD, so no scan", file=err)
    json_path, md_path = write_approvals(directory, scan, express_rule=express_rule)
    print(f"wrote {json_path} and {md_path}", file=out)
    return EXIT_OK


def _scan_gate(deps: Deps, out: TextIO, err: TextIO) -> int:
    try:
        root = deps.data_root()
        corpus = load_corpus(root)
    except (FetchError, CuadFormatError, OSError) as exc:
        print(f"refused: {exc}", file=err)
        return EXIT_REFUSED
    summary = summarize_gate(corpus.cases, scan_cases(root, corpus.cases))
    directory = deps.results_dir / (deps.now().strftime("%Y%m%dT%H%M%SZ") + "-scan-gate")
    json_path, md_path = write_gate(directory, summary)
    print(f"wrote {json_path} and {md_path}", file=out)
    return EXIT_OK


def _liability(args: argparse.Namespace, deps: Deps, out: TextIO, err: TextIO) -> int:
    directory = Path(args.directory)
    baseline = Path(args.baseline) if args.baseline else None
    for path in (directory, baseline):
        if path is not None and not (path / "manifest.json").is_file():
            print(f"refused: {path} is not a benchmark run directory", file=err)
            return EXIT_REFUSED
    try:
        groups = cap_groups(deps.data_root())
    except (FetchError, CuadFormatError, OSError, KeyError) as exc:
        print(f"refused: CUAD labels unavailable ({exc})", file=err)
        return EXIT_REFUSED
    json_path, md_path = write_liability(directory, groups, baseline)
    print(f"wrote {json_path} and {md_path}", file=out)
    return EXIT_OK


def _describe(command: str, cap: Decimal, price: Price, out: TextIO) -> None:
    print(
        f"{command}: model {price.model} at ${price.input_per_mtok}/${price.output_per_mtok} per "
        f"million input/output tokens (prices checked {price.checked_on}), cap ${cap}",
        file=out,
    )


def _data(out: TextIO, err: TextIO) -> int:
    try:
        root = fetch()
        corpus = load_corpus(root)
    except (FetchError, CuadFormatError) as exc:
        print(f"refused: {exc}", file=err)
        return EXIT_REFUSED
    print(
        f"CUAD ready at {root}: {len(corpus.cases)} contracts usable, "
        f"{len(corpus.too_large)} over the Reader's {corpus.max_chars:,}-character limit, "
        f"{len(corpus.missing_text)} without a text file",
        file=out,
    )
    return EXIT_OK
