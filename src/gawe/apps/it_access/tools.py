"""IT-access tools: look up a user, grant access.

`iam.grant_access@2.0.0` carries the user account's revision, an end date (`expires_at`) and the
lifetime it was computed from, all added by trusted preparation. The IAM system accepts the
grant only while the account still has that revision. Version 1.0.0 stays registered (retired)
only to verify grants dispatched before this existed; those grants have no end date.
"""

from collections.abc import Mapping
from typing import Any

from gawe.apps.it_access.grant_preparation import MAX_LIFETIME_HOURS, MIN_LIFETIME_HOURS
from gawe.apps.record_binding import bound, with_snapshots
from gawe.connectors.credentials import Credential, ScopedClient
from gawe.connectors.mock_iam import MockIAM
from gawe.core.executor import ToolCall, ToolImplementation
from gawe.core.fields import FieldClass
from gawe.core.models import ToolManifest
from gawe.core.verifier import PostCondition

EMAIL = {"type": "string", "maxLength": 254}
GRANT_PROPOSAL: dict[str, Any] = {
    "type": "object",
    "properties": {
        "email": EMAIL,
        "system": {"enum": ["wiki", "crm", "billing", "prod_db"]},
        "level": {"enum": ["read", "write", "admin"]},
    },
    "required": ["email", "system", "level"],
    "additionalProperties": False,
}
GRANT_OUTPUT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "grant_id": {"type": "string"},
        "email": {"type": "string"},
        "system": {"type": "string"},
        "level": {"type": "string"},
    },
    "required": ["grant_id", "email", "system", "level"],
}
_BOUND_GRANT = bound(GRANT_PROPOSAL, "user_version")
GRANT_INPUT: dict[str, Any] = {
    **_BOUND_GRANT,
    "properties": {
        **_BOUND_GRANT["properties"],
        "expires_at": {
            "type": "string",
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        },
        # Schema bounds match preparation; policy also denies over the tenant maximum.
        "lifetime_hours": {
            "type": "integer",
            "minimum": MIN_LIFETIME_HOURS,
            "maximum": MAX_LIFETIME_HOURS,
        },
    },
    "required": [*_BOUND_GRANT["required"], "expires_at", "lifetime_hours"],
}

GRANT_FIELDS: dict[str, FieldClass] = {"email": "typed", "system": "typed", "level": "typed"}

MANIFESTS: tuple[ToolManifest, ...] = (
    ToolManifest(
        name="iam.lookup_user",
        writes=(),
        version="1.0.0",
        field_classes={"email": "typed"},
        description="Look up a company user by email: name, department and manager.",
        input_schema={
            "type": "object",
            "properties": {"email": EMAIL},
            "required": ["email"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "name": {"type": "string"},
                "department": {"type": "string"},
            },
            "required": ["email", "name", "department"],
        },
        side_effect_class="read",
        risk_tier="low",
    ),
    ToolManifest(
        name="iam.grant_access",
        writes=("iam.grant",),
        version="1.0.0",
        description="Grant a user access to a system at a given level.",
        input_schema=GRANT_PROPOSAL,
        field_classes=GRANT_FIELDS,
        output_schema=GRANT_OUTPUT,
        side_effect_class="write",
        risk_tier="high",
        required_roles=("it_agent",),
        postcondition="iam.grant_matches",
        retired=True,
    ),
    ToolManifest(
        name="iam.grant_access",
        writes=("iam.grant",),
        version="2.0.0",
        description="Grant a user access to a system at a given level.",
        preparation_required=True,
        record_binding="iam.user",
        proposal_schema=GRANT_PROPOSAL,
        input_schema=GRANT_INPUT,
        field_classes={
            **GRANT_FIELDS,
            "record.user_version": "typed",
            "expires_at": "typed",
            "lifetime_hours": "typed",
        },
        output_schema=GRANT_OUTPUT,
        side_effect_class="write",
        risk_tier="high",
        required_roles=("it_agent",),
        postcondition="iam.grant_matches",
    ),
)

POSTCONDITIONS: Mapping[str, PostCondition] = {
    "iam.grant_matches": lambda args, out: (
        out.get("email") == args.get("email")
        and out.get("system") == args.get("system")
        and out.get("level") == args.get("level")
    ),
}


# What each tool's credential may do (M23): only what the tool itself does.
TOOL_SCOPES: dict[str, frozenset[str]] = {
    "iam.lookup_user": frozenset({"user:read"}),
    "iam.grant_access": frozenset({"grant:write"}),
}
WRITE_SCOPES: dict[str, str] = {"iam.grant": "grant:write"}


def tool_clients(iam: MockIAM) -> dict[str, ScopedClient]:
    """Each tool's client: the IAM system, bound to the tool's own credential."""
    return {
        tool: ScopedClient(iam, Credential(f"gawe-tool:{tool}", scopes))
        for tool, scopes in TOOL_SCOPES.items()
    }


def implementations(iam: MockIAM) -> dict[str, ToolImplementation]:
    clients = tool_clients(iam)

    def lookup(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        return clients["iam.lookup_user"].lookup_user(args["email"])  # type: ignore[no-any-return]

    def grant(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        return clients["iam.grant_access"].grant_access(  # type: ignore[no-any-return]
            call.idempotency_key,
            args["email"],
            args["system"],
            args["level"],
            user_version=args["record"]["user_version"],
            expires_at=args["expires_at"],
            lifetime_hours=args["lifetime_hours"],
            run_id=call.run_id,
        )

    return {"iam.lookup_user": lookup, "iam.grant_access": grant}


# M27: versioned snapshots; preceding versions still serve persisted plans/approvals.

MANIFESTS += (
    with_snapshots(
        next(m for m in MANIFESTS if m.name == "iam.grant_access" and m.version == "2.0.0"),
        "3.0.0",
        maximum=1,
    ),
)
