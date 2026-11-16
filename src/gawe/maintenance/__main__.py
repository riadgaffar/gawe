"""One maintenance pass against the configured durable stores.

    python -m gawe.maintenance sweep --once        (or: make sweep)

Reads the same settings as the API: GAWE_APP (whose bundle reopens the mock-system state in
GAWE_DEMO_DIR), GAWE_POLICY_SIGNING_KEY, the database variables and the maintenance settings.
GAWE_STORE must be `postgres`: a standalone process with its own in-memory store would see none of
the API's work, so `memory` is refused. No model provider is constructed and nothing is planned;
continuation drives only plans that already exist, through the normal executor gates.

Prints one JSON heartbeat per job kind. Exit status: 0 when every heartbeat is ok, 1 when a tick
failed, 2 for a configuration error.
"""

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from gawe.anchoring.config import AnchorConfigError, sinks_from_env
from gawe.api.main import ConfigError, _load_bundle, _required, review_key_from_env
from gawe.api.wiring import Stores, build_engine, postgres_stores
from gawe.core.policy.signing import PolicySigner, PolicySigningError
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


def sweep_once(clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> int:
    try:
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
    except (ConfigError, SettingsError, AnchorConfigError, PolicySigningError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    beats = engine.runner.tick()
    for beat in beats:
        print(json.dumps(beat.fields(), sort_keys=True))
    return 0 if all(beat.status == "ok" for beat in beats) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gawe.maintenance")
    commands = parser.add_subparsers(dest="command", required=True)
    sweep = commands.add_parser("sweep", help="resolve due maintenance jobs")
    sweep.add_argument(
        "--once",
        action="store_true",
        help="run a single pass and exit (the API process provides the recurring schedule)",
    )
    args = parser.parse_args(argv)
    if not args.once:
        parser.error("only `sweep --once` is supported; the API runs the recurring schedule")
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    return sweep_once()


if __name__ == "__main__":
    raise SystemExit(main())
