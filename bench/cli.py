"""Command line for the benchmark.

    python -m bench data
    python -m bench estimate --max-usd 2 [--trial]
    python -m bench run --max-usd 25 [--yes] [--accept-ceiling-over-cap] [--estimate-from FILE]
    python -m bench run --max-usd 15 --resume bench-results/<run id> [--estimate-from FILE]
    python -m bench report bench-results/<run id>
    python -m bench scripted

`scripted` runs every attack with a model fooled on purpose (M13): no model is called, nothing is
spent, so it takes no cap and no model. It writes a run directory and its report.

Every command that may call a real model needs --max-usd: there is no default and no
environment fallback, so a cap is always a deliberate choice. The model comes from --model or
GAWE_LLM_MODEL and must have a row in bench/prices.yaml.

`estimate` without --trial spends nothing: it uses the free token-counting endpoint. With
--trial, --max-usd caps the trial's spend.

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

from bench.budget import Price, UnknownModelError, parse_usd, price_for
from bench.cuad.fetch import FetchError, fetch
from bench.cuad.labels import CuadFormatError, load_corpus
from bench.cuad.run import PROFILE_ORDER
from bench.estimate import EstimateMismatchError
from bench.harness import (
    DEFAULT_SEED,
    Deps,
    ResumeMismatchError,
    Settings,
    run_benchmark,
    run_estimate,
    run_scripted_suite,
)
from bench.report import write_report
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

    report = commands.add_parser("report", help="write report.json and report.md for a run")
    report.add_argument("directory", help="a run directory under bench-results/")
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
    if args.command == "scripted":
        directory = run_scripted_suite(deps or Deps(), out)
        return _report(directory, out, err)
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
    )


def _report(directory: Path, out: TextIO, err: TextIO) -> int:
    if not (directory / "manifest.json").is_file():
        print(f"refused: {directory} is not a benchmark run directory", file=err)
        return EXIT_REFUSED
    json_path, md_path = write_report(directory)
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
