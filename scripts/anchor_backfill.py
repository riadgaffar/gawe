"""Anchor finished runs that have no anchor yet, in every sink GAWE_ANCHOR_SINKS names.

Run after an outage of an anchor sink, or after turning anchoring on:

    make anchor-backfill

Reads the same database settings as the API (see gawe.db.session). Safe to run repeatedly: a run
already anchored in every sink is left alone.
"""

import argparse
import sys

from gawe.anchoring.backfill import DEFAULT_LIMIT, backfill
from gawe.anchoring.config import AnchorConfigError, sinks_from_env
from gawe.api.wiring import postgres_stores
from gawe.core.anchor import Anchorer
from gawe.core.audit import AuditLog
from gawe.db.session import app_database_url, make_engine


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="most runs to look at")
    args = parser.parse_args(argv)
    try:
        sinks = sinks_from_env()
    except AnchorConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not sinks:
        print("GAWE_ANCHOR_SINKS is not set; nothing to anchor to", file=sys.stderr)
        return 2
    url, _ = app_database_url()
    stores = postgres_stores(make_engine(url))
    filled = backfill(stores.runs, Anchorer(AuditLog(stores.audit), sinks), limit=args.limit)
    print(f"anchored {len(filled)} run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
