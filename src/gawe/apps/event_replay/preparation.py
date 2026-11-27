"""Trusted preparation for event replay (M25): the run's tenant, and the batch frozen as the
billing platform holds it.

Everything policy and the approver judge comes from here, never from the proposal: the tenant
from the run, and each event's customer, amount, destination, delivery key and revision from the
platform's own record. A proposal carries event ids only.
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from gawe.apps.record_binding import EVENT_FIELDS, capture, utc_now
from gawe.connectors.base import ConnectorError
from gawe.connectors.mock_billing import MockBilling
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.preparation import PreparationError, PreparedAction
from gawe.core.runs import RunRecord

FROZEN = ("event_id", "customer", "amount", "destination", "delivery_key")


def _tenant(run: RunRecord | None) -> str:
    if run is None:
        raise PreparationError("preparation_no_run")  # fail closed: no run, no tenant
    return run.tenant_id


class ListPreparer:
    """The list reads one tenant's queue: the run's."""

    def __call__(
        self, args: Mapping[str, Any], *, reapproval: bool, run: RunRecord | None = None
    ) -> PreparedAction:
        return PreparedAction(args={**args, "tenant_id": _tenant(run)}, metadata={})


class ReplayPreparer:
    """Freeze the batch. Fails closed on an event of another tenant, an unknown one, one no
    longer failed, a duplicate or an empty batch. An event already delivered under its own key
    (its original delivery landed late) is left out and listed: it needs no replay."""

    def __init__(
        self,
        billing: MockBilling,
        *,
        snapshots: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._billing = billing
        self.snapshots, self.clock = snapshots, clock

    def __call__(
        self, args: Mapping[str, Any], *, reapproval: bool, run: RunRecord | None = None
    ) -> PreparedAction:
        tenant = _tenant(run)
        ids = list(args.get("event_ids") or ())
        if not ids or len(set(ids)) != len(ids):
            raise PreparationError("batch_invalid")
        batch: list[dict[str, Any]] = []
        delivered: list[str] = []
        snapshots: list[dict[str, Any]] = []
        for event_id in ids:
            try:
                record, revision = self._billing.event(event_id)
            except ConnectorError as exc:
                raise PreparationError(exc.code) from None
            if record["tenant_id"] != tenant:
                # The same code as an unknown event would leak that the id exists elsewhere.
                raise PreparationError("event_not_found")
            if self._billing.delivered(record["delivery_key"]):
                delivered.append(event_id)
                continue
            if record["status"] != "failed":
                raise PreparationError("event_not_failed")
            if self.snapshots:
                try:
                    snapshots.append(capture(EVENT_FIELDS, event_id, record, revision, self.clock))
                except Exception as exc:
                    raise PreparationError("record_version_unavailable") from exc
            batch.append({**{k: record[k] for k in FROZEN}, "revision": revision})
        if not batch:
            raise PreparationError("nothing_to_replay")
        customers = sorted({e["customer"] for e in batch})
        prepared = {
            "event_ids": ids,
            "tenant_id": tenant,
            "batch": batch,
            "count": len(batch),
            "total": sum(int(e["amount"]) for e in batch),
            "customers": customers,
            "customer_count": len(customers),
            "already_delivered": delivered,
            "record": {
                "batch_revision": sha256_hex(
                    canonical_json([[e["event_id"], e["revision"]] for e in batch])
                )
            },
        }
        if self.snapshots:
            prepared["snapshots"] = snapshots
        return PreparedAction(args=prepared, metadata={})
