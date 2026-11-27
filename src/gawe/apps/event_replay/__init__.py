"""Event replay: replay a tenant's failed (dead-lettered) billing events, as a batch frozen at
preview (M25).

An operator asks, say, "replay the failed invoice.paid events for customer X since the outage".
The agent lists them and proposes their ids. Preparation freezes what each id means from the
billing platform's records; policy judges the count, the total, the customers and what is left
of the day's limit; the platform applies the batch whole or not at all, never twice, and only
while every event is as it was frozen. The model never sees an event's payload, which whoever
sent the webhook wrote. There is no Reader: the run works from the operator's objective.

Run with `GAWE_APP=gawe.apps.event_replay:bundle`.
"""

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gawe.api.wiring import AppBundle
from gawe.apps.event_replay.preparation import ListPreparer, ReplayPreparer
from gawe.apps.event_replay.tools import (
    MANIFESTS,
    POSTCONDITIONS,
    TOOL_SCOPES,
    WRITE_SCOPES,
    implementations,
)
from gawe.apps.refusal_observers import events as refusal_observers
from gawe.connectors.base import Ledger
from gawe.connectors.mock_billing import MockBilling
from gawe.core import tenant_settings
from gawe.core.policy.provenance import PolicySource, PolicyVariable
from gawe.core.readback import ReadbackAdapter, ReadbackResult, compare
from gawe.core.runs import RunRecord
from gawe.core.tenant_settings import TenantSettings

HERE = Path(__file__).parent
PROFILE_NAME = "event_replay"
REPLAY = ("billing.replay_events", "1.0.0")
DAILY_REPLAY_LIMIT = 50_000  # the billing platform's per-tenant setting, in whole units
DEFAULT_SETTINGS: Mapping[str, Any] = {
    "automatic_events": 10,
    "automatic_total": 1_000,
    "approval_events": 100,
    "approval_total": 25_000,
}
TENANT_VARIABLES: Mapping[str, Any] = {"tenant": DEFAULT_SETTINGS}
# Per-tenant settings (M29); the defaults above are what every tenant had before. The daily limit
# is the billing platform's own setting, not GAWE's.
_COUNT = {"type": "integer", "minimum": 0}
SETTINGS_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {name: _COUNT for name in DEFAULT_SETTINGS},
    "required": sorted(DEFAULT_SETTINGS),
    "additionalProperties": False,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def bundle(
    demo_dir: Path | None = None,
    *,
    billing: MockBilling | None = None,
    clock: Callable[[], datetime] = _utc_now,
    settings: TenantSettings | None = None,
) -> AppBundle:
    """The app. `settings` default to `GAWE_TENANT_SETTINGS` over `DEFAULT_SETTINGS`."""
    settings = settings or tenant_settings.from_env(SETTINGS_SCHEMA, DEFAULT_SETTINGS)
    directory = demo_dir or Path(os.environ.get("GAWE_DEMO_DIR", ".demo-data"))
    billing = billing or MockBilling.from_seed(
        Ledger(directory / "ledger.jsonl"),
        daily_limits={},
        default_daily_limit=DAILY_REPLAY_LIMIT,
        clock=clock,
    )
    return AppBundle(
        manifests=MANIFESTS,
        refusal_observers=refusal_observers(billing, MANIFESTS),
        tools=implementations(billing),
        readbacks={(REPLAY[0], v): readback(billing) for v in ("1.0.0", "2.0.0")},
        preparers={
            ("events.list_failed", "1.0.0"): ListPreparer(),
            REPLAY: ReplayPreparer(billing),
            (REPLAY[0], "2.0.0"): ReplayPreparer(billing, snapshots=True, clock=clock),
        },
        tool_scopes=TOOL_SCOPES,
        write_scopes=WRITE_SCOPES,
        guarantees=billing.GUARANTEES,
        policy_rules=HERE / "policies.yaml",
        profiles=frozenset({PROFILE_NAME}),
        variables=variables_for(billing, settings),
        postconditions=POSTCONDITIONS,
        tenant_settings=settings,
        policy_sources={
            "tenant_config": PolicySource(kind="configuration", resource="config.tenant"),
            # The day's running total, kept by the platform. Replays use it up, so it can only
            # add denials; a write judged by it needs the platform's reservation (M22).
            "daily_total": PolicySource(kind="counter", resource="billing.daily_total"),
        },
        policy_variables={
            "tenant.automatic_events": PolicyVariable(dependencies=("tenant_config",)),
            "tenant.automatic_total": PolicyVariable(dependencies=("tenant_config",)),
            "tenant.approval_events": PolicyVariable(dependencies=("tenant_config",)),
            "tenant.approval_total": PolicyVariable(dependencies=("tenant_config",)),
            "billing.remaining_today": PolicyVariable(dependencies=("daily_total",)),
        },
    )


def variables_for(
    billing: MockBilling, settings: TenantSettings | None = None
) -> Callable[[RunRecord | None], Mapping[str, Any]]:
    """Tenant settings, and what is left of the run's tenant's daily limit. With no run, or a
    platform that sets no limit, it is absent, and a replay denies."""
    settings = settings or TenantSettings(SETTINGS_SCHEMA, DEFAULT_SETTINGS)

    def variables(run: RunRecord | None) -> Mapping[str, Any]:
        remaining = billing.remaining_today(run.tenant_id) if run is not None else None
        found = {} if remaining is None else {"remaining_today": remaining}
        tenant = settings.for_tenant(run.tenant_id if run is not None else None)
        return {"tenant": tenant, "billing": found}

    return variables


def readback(billing: MockBilling) -> ReadbackAdapter:
    """The batch by GAWE's key, and each event's delivery by its own key: every event in the
    batch was either applied by this operation or already delivered by another path."""

    def read(key: str, args: Mapping[str, Any], now: datetime) -> ReadbackResult:
        record = billing.operations.read(key, now)
        expected = {
            "operation_key": key,
            "tenant_id": args["tenant_id"],
            "events": sorted(e["event_id"] for e in args["batch"]),
            "deliveries": sorted(e["delivery_key"] for e in args["batch"]),
        }
        observed = None
        output = None
        if record is not None:
            applied, skipped = list(record["applied"]), list(record["already_delivered"])
            delivered = [
                e["delivery_key"]
                for e in args["batch"]
                if (d := billing.delivery(e["delivery_key"])) is not None
                and (d["via"] == key) == (e["event_id"] in applied)
            ]
            observed = {
                "operation_key": record["operation_key"],
                "tenant_id": record["tenant_id"],
                "events": sorted([*applied, *skipped]),
                "deliveries": sorted(delivered),
            }
            output = {
                "batch_id": record["batch_id"],
                "applied": applied,
                "already_delivered": skipped,
                "total": record["total"],
            }
        return compare(expected, observed, output=output)

    return read
