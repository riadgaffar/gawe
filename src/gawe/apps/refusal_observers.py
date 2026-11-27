"""Trusted diagnostic reads, with no write client, model input or target-history lookup."""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from gawe.apps.record_binding import ACCOUNT_FIELDS, CONTRACT_FIELDS, EVENT_FIELDS
from gawe.connectors.base import ConnectorError
from gawe.connectors.mock_billing import MockBilling
from gawe.connectors.mock_clm import MockCLM
from gawe.connectors.mock_iam import MockIAM
from gawe.core.models import ToolManifest
from gawe.core.refusals import RefusalObserver
from gawe.core.runs import RunRecord


def _read(
    read: Callable[[str], tuple[dict[str, Any], str]], subject: str
) -> tuple[Mapping[str, Any], str] | None:
    try:
        return read(subject)
    except ConnectorError as exc:
        if exc.code in {"contract_not_found", "user_not_found", "event_not_found"}:
            return None
        raise


def contracts(
    clm: MockCLM, manifests: Sequence[ToolManifest]
) -> dict[tuple[str, str], RefusalObserver]:
    clm.contracts.observation_spec = CONTRACT_FIELDS
    observer = RefusalObserver(
        CONTRACT_FIELDS,
        lambda args: (args["contract_id"],),
        lambda subject, run: _read(clm.current_contract, subject),
        revisions=lambda args: {args["contract_id"]: args["record"]["contract_version"]},
    )
    return {(m.name, m.version): observer for m in manifests if m.record_binding == "clm.contract"}


def accounts(
    iam: MockIAM, manifests: Sequence[ToolManifest]
) -> dict[tuple[str, str], RefusalObserver]:
    iam.users.observation_spec = ACCOUNT_FIELDS
    observer = RefusalObserver(
        ACCOUNT_FIELDS,
        lambda args: (args["email"].lower(),),
        lambda subject, run: _read(iam.users.get, subject),
        revisions=lambda args: {args["email"].lower(): args["record"]["user_version"]},
    )
    return {(m.name, m.version): observer for m in manifests if m.record_binding == "iam.user"}


def events(
    billing: MockBilling, manifests: Sequence[ToolManifest]
) -> dict[tuple[str, str], RefusalObserver]:
    billing.events.observation_spec = EVENT_FIELDS

    def read(subject: str, run: RunRecord) -> tuple[Mapping[str, Any], str] | None:
        row = _read(billing.event, subject)
        if row is None or row[0].get("tenant_id") != run.tenant_id:
            return None  # No fields or revision from a record now outside this tenant.
        return row

    observer = RefusalObserver(
        EVENT_FIELDS,
        lambda args: tuple(e["event_id"] for e in args["batch"]),
        read,
        revisions=lambda args: {e["event_id"]: e["revision"] for e in args["batch"]},
    )
    return {(m.name, m.version): observer for m in manifests if m.record_binding == "billing.event"}
