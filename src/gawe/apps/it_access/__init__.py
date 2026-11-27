"""IT access requests: a small second workflow showing the engine is not contract-specific.

Since M24 policy also sees the grantee's earlier grants (`history.*`, see `grant_history`): a
system granted to one person `automatic_grants` times within `history_days` needs security the
next time, so access can't be collected by asking again and again.

Run with `GAWE_APP=gawe.apps.it_access:bundle`.
"""

import json
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeGuard

from gawe.api.wiring import AppBundle
from gawe.apps.it_access.grant_preparation import DEFAULT_LIFETIME_HOURS, GrantPreparer
from gawe.apps.it_access.reader_schema import PROFILE
from gawe.apps.it_access.tools import (
    MANIFESTS,
    POSTCONDITIONS,
    TOOL_SCOPES,
    WRITE_SCOPES,
    implementations,
)
from gawe.apps.readback import operation_adapter
from gawe.apps.refusal_observers import accounts as refusal_observers
from gawe.connectors.base import Ledger
from gawe.connectors.mock_iam import MockIAM
from gawe.connectors.mock_tickets import MockTickets, Ticket
from gawe.core import tenant_settings
from gawe.core.guarantees import DispatchCondition
from gawe.core.history import HistoryEntry, HistorySubject
from gawe.core.policy.provenance import PolicySource, PolicyVariable
from gawe.core.policy.rules import email_domain
from gawe.core.readback import ReadbackAdapter
from gawe.core.records import StepRecord
from gawe.core.runs import RunRecord
from gawe.core.tenant_settings import TenantSettings

HERE = Path(__file__).parent
PROFILE_NAME = "it_access"
GRANT = "iam.grant_access"
MAX_GRANT_LIFETIME_HOURS = 30 * 24
DEFAULT_SETTINGS: Mapping[str, Any] = {
    "internal_domains": ["acme.example"],
    "max_grant_lifetime_hours": MAX_GRANT_LIFETIME_HOURS,
    # Systems that hold another app's controls (M23): contract review's deal values, which
    # decide whether a signature needs finance, live in the CRM; event replay's events,
    # amounts, daily total and limit live in billing (M25). Changing them takes a person.
    "control_systems": ["crm", "billing"],
    # M24: how many grants of one system to one person run automatically within the window;
    # the next one needs security. Counted from GAWE's own records, every level alike.
    "automatic_grants": 2,
    "history_days": 30,
}
TENANT_VARIABLES: Mapping[str, Any] = {"tenant": DEFAULT_SETTINGS}
# Per-tenant settings (M29). Defaults are the values above, so a deployment without a settings
# file decides as before. grant_history and policy still treat an unusable value as missing.
_NAMES = {"type": "array", "items": {"type": "string", "minLength": 1}, "uniqueItems": True}
_POSITIVE = {"type": "integer", "minimum": 1}
SETTINGS_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "internal_domains": _NAMES,
        "max_grant_lifetime_hours": _POSITIVE,
        "control_systems": _NAMES,
        "automatic_grants": _POSITIVE,
        "history_days": _POSITIVE,
    },
    "required": [
        "internal_domains",
        "max_grant_lifetime_hours",
        "control_systems",
        "automatic_grants",
        "history_days",
    ],
    "additionalProperties": False,
}


def _positive(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def grant_history(
    settings: Mapping[str, Any],
) -> Callable[[Sequence[HistoryEntry], Mapping[str, Any], RunRecord], dict[str, list[str]]]:
    """`repeated_systems`: the systems this grantee has been granted `automatic_grants` times or
    more in the `history_days` before this run started, whatever the level and whether or not a
    person approved them. A missing or unusable setting gives no variable, so every grant denies.
    The window ends at the judged run's start, so the daily review judges a grant as it was."""

    def variables(
        entries: Sequence[HistoryEntry], args: Mapping[str, Any], run: RunRecord
    ) -> dict[str, list[str]]:
        allowed, days = settings.get("automatic_grants"), settings.get("history_days")
        if not (_positive(allowed) and _positive(days)):
            return {}
        since = run.created_at - timedelta(days=days)
        counts = Counter(e.resolved_args.get("system") for e in entries if e.started_at >= since)
        return {
            "repeated_systems": sorted(
                str(system) for system, n in counts.items() if system and n >= allowed
            )
        }

    return variables


def grant_history_for(
    settings: TenantSettings,
) -> Callable[[Sequence[HistoryEntry], Mapping[str, Any], RunRecord], dict[str, list[str]]]:
    """`grant_history` with the judged run's tenant's settings."""

    def variables(
        entries: Sequence[HistoryEntry], args: Mapping[str, Any], run: RunRecord
    ) -> dict[str, list[str]]:
        return grant_history(settings.for_tenant(run.tenant_id))(entries, args, run)

    return variables


def grant_subject(settings: TenantSettings) -> HistorySubject:
    return HistorySubject(
        argument="email",
        lower=True,
        variables=grant_history_for(settings),
        shown=("system", "level"),
    )


# Every tenant's defaults, for callers outside a bundle (tests, benchmarks).
GRANT_HISTORY = HistorySubject(
    argument="email",
    lower=True,
    variables=grant_history(TENANT_VARIABLES["tenant"]),
    shown=("system", "level"),
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def sample_tickets(path: Path = HERE / "samples" / "tickets.json") -> list[Ticket]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Ticket(
            tenant_id=data["tenant_id"],
            ticket_id=t["ticket_id"],
            submitted_by=t["submitted_by"],
            channel=t["channel"],
            status="open",
            body=(path.parent / t["file"]).read_text(encoding="utf-8"),
        )
        for t in data["tickets"]
    ]


def ticket_open(tickets: MockTickets) -> DispatchCondition:
    """The run's ticket is still open in the ticket system. A run without one can't hold it."""

    def check(run: RunRecord, step: StepRecord) -> bool:
        if run.source_id is None:
            return False
        return tickets.get(run.tenant_id, run.source_id).status == "open"

    return DispatchCondition("ticket_open", check)


def bundle(
    demo_dir: Path | None = None,
    *,
    iam: MockIAM | None = None,
    tickets: MockTickets | None = None,
    grant_lifetime_hours: int = DEFAULT_LIFETIME_HOURS,
    clock: Callable[[], datetime] = _utc_now,
    settings: TenantSettings | None = None,
) -> AppBundle:
    """The app. `settings` default to `GAWE_TENANT_SETTINGS` over `DEFAULT_SETTINGS`."""
    settings = settings or tenant_settings.from_env(SETTINGS_SCHEMA, DEFAULT_SETTINGS)
    directory = demo_dir or Path(os.environ.get("GAWE_DEMO_DIR", ".demo-data"))
    ledger = Ledger(directory / "ledger.jsonl")
    iam = iam or MockIAM.from_seed(HERE / "samples" / "iam_seed.json", ledger)
    if tickets is None:
        tickets = MockTickets.in_state_dir(ledger.path.parent if ledger.path else None)
        tickets.seed(sample_tickets())
    return AppBundle(
        manifests=MANIFESTS,
        refusal_observers=refusal_observers(iam, MANIFESTS),
        tools=implementations(iam),
        readbacks=readbacks(iam),
        preparers={
            ("iam.grant_access", "3.0.0"): GrantPreparer(
                iam,
                lifetime_hours=grant_lifetime_hours,
                clock=clock,
                tickets=tickets,
                snapshots=True,
            ),
            ("iam.grant_access", "2.0.0"): GrantPreparer(
                iam, lifetime_hours=grant_lifetime_hours, clock=clock, tickets=tickets
            ),
        },
        tool_scopes=TOOL_SCOPES,
        write_scopes=WRITE_SCOPES,
        guarantees=iam.GUARANTEES,
        # The ticket lives in another system, so the IAM write can't check it: it is read again
        # just before a grant is sent (M22).
        dispatch_conditions={
            ("iam.grant_access", v): (ticket_open(tickets),) for v in ("2.0.0", "3.0.0")
        },
        # 1.0.0 grants have no end date; GAWE ends them on one it sets (M21).
        revokers={("iam.grant_access", v): iam for v in ("1.0.0", "2.0.0", "3.0.0")},
        grant_lifetime=timedelta(hours=grant_lifetime_hours),
        # Runs read their ticket from the ticket system; uploads are refused.
        sources={PROFILE_NAME: tickets},
        policy_rules=HERE / "policies.yaml",
        profiles=frozenset({PROFILE_NAME}),
        reader_profile=PROFILE,
        variables=variables_for(tickets, settings),
        postconditions=POSTCONDITIONS,
        history={GRANT: grant_subject(settings)},
        tenant_settings=settings,
        policy_sources={
            "tenant_config": PolicySource(kind="configuration", resource="config.tenant"),
            "reader_findings": PolicySource(kind="findings", resource="run.findings"),
            "ticket_system": PolicySource(kind="resource", resource="tickets.ticket"),
            "earlier_grants": PolicySource(kind="history", resource=f"gawe.history.{GRANT}"),
        },
        policy_variables={
            "tenant.internal_domains": PolicyVariable(dependencies=("tenant_config",)),
            "tenant.max_grant_lifetime_hours": PolicyVariable(dependencies=("tenant_config",)),
            "tenant.control_systems": PolicyVariable(dependencies=("tenant_config",)),
            "request.reported_requester": PolicyVariable(dependencies=("reader_findings",)),
            "request.submitters": PolicyVariable(dependencies=("ticket_system",)),
            "request.portal_submitters": PolicyVariable(dependencies=("ticket_system",)),
            "history.repeated_systems": PolicyVariable(
                dependencies=("earlier_grants", "tenant_config")
            ),
        },
    )


def readbacks(iam: MockIAM) -> dict[tuple[str, str], ReadbackAdapter]:
    """Version-specific read-back. 1.0.0 reads the older store, for earlier dispatches only."""
    adapters = {
        ("iam.grant_access", "1.0.0"): operation_adapter(
            iam.legacy_operations,
            ("email", "system", "level"),
            ("grant_id", "email", "system", "level"),
        ),
        ("iam.grant_access", "2.0.0"): operation_adapter(
            iam.operations,
            ("email", "system", "level", "record", "expires_at", "lifetime_hours"),
            ("grant_id", "email", "system", "level"),
        ),
    }

    adapters[("iam.grant_access", "3.0.0")] = adapters[("iam.grant_access", "2.0.0")]
    return adapters


def request_variables(findings: Mapping[str, Any] | None) -> dict[str, str]:
    """The Reader's reported requester, unchanged; unusable findings cannot authorize a grant.

    It binds the grantee to what the Reader reported (M15). It is attacker-influenced text, so
    it is a cross-check only; who filed the ticket comes from the ticket system.
    """
    requester = (findings or {}).get("requester_email")
    if not isinstance(requester, str) or email_domain(requester) is None:
        return {}
    return {"reported_requester": requester}


def ticket_variables(run: RunRecord | None, tickets: MockTickets) -> dict[str, list[str]]:
    """Who filed the run's ticket, from the ticket system's own record (M16c).

    `submitters` is the filer. `portal_submitters` is the filer only when the ticket came through
    the signed-in portal; an email or public-form "submitter" is whatever the sender wrote, so the
    list is empty. Without a readable ticket both are absent, and policy denies.
    """
    if run is None or run.source_id is None:
        return {}
    try:
        ticket = tickets.get(run.tenant_id, run.source_id)
    except Exception:  # noqa: BLE001 - an unreadable ticket leaves policy without its inputs
        return {}
    trusted = [ticket.submitted_by] if ticket.channel == "portal" else []
    return {"submitters": [ticket.submitted_by], "portal_submitters": trusted}


def variables_for(
    tickets: MockTickets, settings: TenantSettings | None = None
) -> Callable[[RunRecord | None], Mapping[str, Any]]:
    settings = settings or TenantSettings(SETTINGS_SCHEMA, DEFAULT_SETTINGS)

    def variables(run: RunRecord | None) -> Mapping[str, Any]:
        return {
            "tenant": settings.for_tenant(run.tenant_id if run is not None else None),
            "request": {
                **request_variables(run.findings if run is not None else None),
                **ticket_variables(run, tickets),
            },
        }

    return variables
