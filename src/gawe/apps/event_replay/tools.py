"""Event replay tools: list the tenant's failed events, replay a frozen batch of them (M25).

Both tools are prepared. Preparation adds the run's tenant, which no proposal can carry, so a
plan can't name another tenant. For the replay, it also freezes each event as the platform holds
it: revision, customer, amount, destination and delivery key, and the batch's count, total and
customers. Policy judges those, the approval is bound to them, and the billing platform applies
the batch only while every event still has the revision it was prepared against.
"""

from collections.abc import Mapping
from typing import Any

from gawe.apps.record_binding import record_schema, with_snapshots
from gawe.connectors.credentials import Credential, ScopedClient
from gawe.connectors.mock_billing import MockBilling
from gawe.core.executor import ToolCall, ToolImplementation
from gawe.core.fields import FieldClass
from gawe.core.models import ToolManifest
from gawe.core.verifier import PostCondition

MAX_BATCH = 200  # the schema's bound; policy denies far below it
ID = {"type": "string", "pattern": r"^[A-Za-z0-9._:-]{1,64}$"}
TIME = {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"}
TENANT = {"type": "string", "minLength": 1, "maxLength": 64}

LIST_PROPOSAL: dict[str, Any] = {
    "type": "object",
    "properties": {"customer": ID, "type": ID, "since": TIME, "until": TIME},
    "additionalProperties": False,
}
LIST_INPUT: dict[str, Any] = {
    **LIST_PROPOSAL,
    "properties": {**LIST_PROPOSAL["properties"], "tenant_id": TENANT},
    "required": ["tenant_id"],
}
LISTED_EVENT = {
    "type": "object",
    "properties": {
        "event_id": {"type": "string"},
        "type": {"type": "string"},
        "customer": {"type": "string"},
        "amount": {"type": "integer"},
        "failed_at": {"type": "string"},
        "status": {"type": "string"},
    },
    "required": ["event_id", "type", "customer", "amount", "failed_at", "status"],
    "additionalProperties": False,
}
LIST_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "items": LISTED_EVENT},
        "event_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["events", "event_ids"],
}

REPLAY_PROPOSAL: dict[str, Any] = {
    "type": "object",
    "properties": {
        "event_ids": {"type": "array", "items": ID, "minItems": 1, "maxItems": MAX_BATCH},
    },
    "required": ["event_ids"],
    "additionalProperties": False,
}
FROZEN_EVENT = {
    "type": "object",
    "properties": {
        "event_id": ID,
        "customer": ID,
        "amount": {"type": "integer", "minimum": 0},
        "destination": ID,
        "delivery_key": ID,
        "revision": {"type": "string", "minLength": 1, "maxLength": 64},
    },
    "required": ["event_id", "customer", "amount", "destination", "delivery_key", "revision"],
    "additionalProperties": False,
}
REPLAY_INPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        **REPLAY_PROPOSAL["properties"],
        "tenant_id": TENANT,
        "batch": {
            "type": "array",
            "items": FROZEN_EVENT,
            "minItems": 1,
            "maxItems": MAX_BATCH,
        },
        "count": {"type": "integer", "minimum": 1, "maximum": MAX_BATCH},
        "total": {"type": "integer", "minimum": 0},
        "customers": {"type": "array", "items": ID, "minItems": 1},
        "customer_count": {"type": "integer", "minimum": 1},
        "already_delivered": {"type": "array", "items": ID},
        "record": record_schema("batch_revision"),
    },
    "required": [
        "event_ids",
        "tenant_id",
        "batch",
        "count",
        "total",
        "customers",
        "customer_count",
        "already_delivered",
        "record",
    ],
    "additionalProperties": False,
}
REPLAY_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "batch_id": {"type": "string"},
        "applied": {"type": "array", "items": {"type": "string"}},
        "already_delivered": {"type": "array", "items": {"type": "string"}},
        "total": {"type": "integer"},
    },
    "required": ["batch_id", "applied", "already_delivered", "total"],
}
REPLAY_FIELDS: dict[str, FieldClass] = {
    "event_ids": "typed",
    "tenant_id": "typed",
    "batch": "typed",
    "count": "typed",
    "total": "typed",
    "customers": "typed",
    "customer_count": "typed",
    "already_delivered": "typed",
    "record.batch_revision": "typed",
}

MANIFESTS: tuple[ToolManifest, ...] = (
    ToolManifest(
        name="events.list_failed",
        writes=(),
        version="1.0.0",
        description=(
            "List this tenant's failed (dead-lettered) events, oldest first, optionally by "
            "customer, event type and a failure-time window (UTC, YYYY-MM-DDTHH:MM:SSZ). Returns "
            "each event's id, type, customer, amount, failure time and status, and `event_ids`."
        ),
        preparation_required=True,
        proposal_schema=LIST_PROPOSAL,
        input_schema=LIST_INPUT,
        field_classes={
            "customer": "typed",
            "type": "typed",
            "since": "typed",
            "until": "typed",
            "tenant_id": "typed",
        },
        output_schema=LIST_OUTPUT,
        side_effect_class="read",
        risk_tier="low",
    ),
    ToolManifest(
        name="billing.replay_events",
        writes=("billing.delivery",),
        version="1.0.0",
        description=(
            "Replay failed events by id, as one batch: each is delivered under its original "
            "delivery key, so an event already delivered is skipped, never billed twice."
        ),
        preparation_required=True,
        record_binding="billing.event",
        proposal_schema=REPLAY_PROPOSAL,
        input_schema=REPLAY_INPUT,
        field_classes=REPLAY_FIELDS,
        output_schema=REPLAY_OUTPUT,
        side_effect_class="write",
        risk_tier="high",
        required_roles=("billing_operator",),
        postcondition="billing.replay_matches",
    ),
)


def _replayed(args: Mapping[str, Any], out: Mapping[str, Any]) -> bool:
    expected = sorted(e["event_id"] for e in args.get("batch", ()))
    reported = sorted([*out.get("applied", ()), *out.get("already_delivered", ())])
    return expected == reported


POSTCONDITIONS: Mapping[str, PostCondition] = {"billing.replay_matches": _replayed}

# What each tool's credential may do (M23): only what the tool itself does.
TOOL_SCOPES: dict[str, frozenset[str]] = {
    "events.list_failed": frozenset({"event:read"}),
    "billing.replay_events": frozenset({"replay:write"}),
}
WRITE_SCOPES: dict[str, str] = {"billing.delivery": "replay:write"}


def tool_clients(billing: MockBilling) -> dict[str, ScopedClient]:
    return {
        tool: ScopedClient(billing, Credential(f"gawe-tool:{tool}", scopes))
        for tool, scopes in TOOL_SCOPES.items()
    }


def implementations(billing: MockBilling) -> dict[str, ToolImplementation]:
    clients = tool_clients(billing)

    def list_failed(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        events = clients["events.list_failed"].list_failed(
            args["tenant_id"],
            customer=args.get("customer"),
            event_type=args.get("type"),
            since=args.get("since"),
            until=args.get("until"),
        )
        return {"events": events, "event_ids": [e["event_id"] for e in events]}

    def replay(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        batch = [
            {k: e[k] for k in ("event_id", "revision", "delivery_key", "amount")}
            for e in args["batch"]
        ]
        return clients["billing.replay_events"].replay(  # type: ignore[no-any-return]
            call.idempotency_key, args["tenant_id"], batch, run_id=call.run_id
        )

    return {"events.list_failed": list_failed, "billing.replay_events": replay}


# M27: versioned snapshots; preceding versions still serve persisted plans/approvals.

MANIFESTS += (
    with_snapshots(
        next(m for m in MANIFESTS if m.name == "billing.replay_events" and m.version == "1.0.0"),
        "2.0.0",
        maximum=200,
    ),
)
