"""One maintenance pass against the configured durable stores.

    python -m gawe.maintenance sweep --once        (or: make sweep)
    python -m gawe.maintenance end-old-grants [--apply] [--notice-days N]

Reads the same settings as the API: GAWE_APP (whose bundle reopens the mock-system state in
GAWE_DEMO_DIR), GAWE_POLICY_SIGNING_KEY, the database variables and the maintenance settings.
GAWE_STORE must be `postgres`: a standalone process with its own in-memory store would see none of
the API's work, so `memory` is refused. No model provider is constructed and nothing is planned;
continuation drives only plans that already exist, through the normal executor gates.

Prints one JSON heartbeat per job kind. Exit status: 0 when every heartbeat is ok, 1 when a tick
failed, 2 for a configuration error.

`end-old-grants` (M21) gives each grant made before M16b an end date: the later of when it was
made plus the app's grant lifetime, and now plus the notice (7 days by default, 1 to 30). Without
`--apply` it only prints one JSON line per grant and a total. With it, it schedules each grant's
expiry job and audits the date; the sweep then revokes at that date. Exit status 0, or 2 for a
configuration error.
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TextIO

from gawe.anchoring.config import AnchorConfigError, sinks_from_env
from gawe.api.main import ConfigError, _load_bundle, _required, review_key_from_env
from gawe.api.wiring import AppBundle, Engine, Stores, build_engine, postgres_stores
from gawe.core.old_grants import DEFAULT_NOTICE_DAYS, notice_period
from gawe.core.policy.signing import PolicySigner, PolicySigningError
from gawe.maintenance.old_grants import OldGrantBackfill
from gawe.maintenance.settings import MaintenanceSettings, SettingsError

log = logging.getLogger("gawe.maintenance")


def _durable_stores() -> Stores:
    store_kind = os.environ.get("GAWE_STORE", "postgres").strip().lower()
    if store_kind == "memory":
        raise ConfigError(
            "GAWE_STORE=memory is ephemeral: a separate process cannot see the API's runs. "
            "The API's own schedule sweeps memory stores; use postgres for `sweep --once`."
        )
    if store_kind != "postgres":
        raise ConfigError("GAWE_STORE must be 'postgres' for a standalone sweep")
    from gawe.db.session import app_database_url, make_engine

    url, _ = app_database_url()
    return postgres_stores(make_engine(url))


def _engine(clock: Callable[[], datetime]) -> tuple[AppBundle, Stores, Engine]:
    """Raises a configuration error (ConfigError, SettingsError, ...) for the caller to report."""
    settings = MaintenanceSettings.from_env()
    bundle = _load_bundle(_required("GAWE_APP"))
    stores = _durable_stores()
    engine = build_engine(
        bundle,
        stores=stores,
        signer=PolicySigner.from_env(),
        provider=None,  # never plan or read documents from a maintenance pass
        anchor_sinks=sinks_from_env(),
        maintenance=settings,
        review_key=review_key_from_env(),
        clock=clock,
    )
    return bundle, stores, engine


_CONFIG_ERRORS = (ConfigError, SettingsError, AnchorConfigError, PolicySigningError)


def sweep_once(clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> int:
    try:
        _, _, engine = _engine(clock)
    except _CONFIG_ERRORS as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    beats = engine.runner.tick()
    for beat in beats:
        print(json.dumps(beat.fields(), sort_keys=True))
    return 0 if all(beat.status == "ok" for beat in beats) else 1


def end_old_grants(
    *,
    apply: bool,
    notice_days: int = DEFAULT_NOTICE_DAYS,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    out: TextIO = sys.stdout,
) -> int:
    try:
        notice_period(notice_days)
        bundle, stores, engine = _engine(clock)
        if bundle.grant_lifetime is None:
            raise ConfigError("this app makes no grants, so it has none to end")
    except (*_CONFIG_ERRORS, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    retired = {(m.name, m.version) for m in bundle.manifests if m.retired}
    backfill = OldGrantBackfill(
        versions=[k for k in bundle.revokers if k in retired],
        lifetime=bundle.grant_lifetime,
        steps=stores.steps,
        executions=stores.executions,
        runs=stores.runs,
        jobs=stores.maintenance,
        audit=engine.audit,
        anchorer=engine.anchorer,
        clock=clock,
    )
    items = backfill.apply(notice_days) if apply else backfill.plan(notice_days)
    for item in items:
        print(json.dumps(item.line(), sort_keys=True), file=out)
    totals = {
        "applied": apply,
        "grants": len(items),
        "new_jobs": sum(not i.job_exists for i in items),
        "by_rule": {r: sum(i.rule == r for i in items) for r in sorted({i.rule for i in items})},
        "notice_days": notice_days,
    }
    print(json.dumps({"total": totals}, sort_keys=True), file=out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gawe.maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    sweep = commands.add_parser("sweep", help="resolve due maintenance jobs")
    sweep.add_argument(
        "--once",
        action="store_true",
        help="run a single pass and exit (the API process provides the recurring schedule)",
    )
    ends = commands.add_parser(
        "end-old-grants", help="give grants made before M16b an end date (dry run by default)"
    )
    ends.add_argument("--apply", action="store_true", help="schedule the expiry jobs and audit")
    ends.add_argument(
        "--notice-days",
        type=int,
        default=DEFAULT_NOTICE_DAYS,
        help="the shortest time any old grant keeps (1 to 30, default 7)",
    )
    args = parser.parse_args(argv)
    if args.command == "end-old-grants":
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        return end_old_grants(apply=args.apply, notice_days=args.notice_days)
    if not args.once:
        parser.error("only `sweep --once` is supported; the API runs the recurring schedule")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return sweep_once()


if __name__ == "__main__":
    raise SystemExit(main())
