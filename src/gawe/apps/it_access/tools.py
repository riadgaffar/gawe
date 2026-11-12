"""IT-access tools: look up a user, grant access."""

from collections.abc import Mapping
from typing import Any

from gawe.connectors.mock_iam import MockIAM
from gawe.core.executor import ToolCall, ToolImplementation
from gawe.core.models import ToolManifest
from gawe.core.verifier import PostCondition

EMAIL = {"type": "string", "maxLength": 254}

MANIFESTS = (
    ToolManifest(
        name="iam.lookup_user",
        writes=(),
        version="1.0.0",
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
        input_schema={
            "type": "object",
            "properties": {
                "email": EMAIL,
                "system": {"enum": ["wiki", "crm", "billing", "prod_db"]},
                "level": {"enum": ["read", "write", "admin"]},
            },
            "required": ["email", "system", "level"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "grant_id": {"type": "string"},
                "email": {"type": "string"},
                "system": {"type": "string"},
                "level": {"type": "string"},
            },
            "required": ["grant_id", "email", "system", "level"],
        },
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


def implementations(iam: MockIAM) -> dict[str, ToolImplementation]:
    def lookup(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        return iam.lookup_user(args["email"])

    def grant(args: Mapping[str, Any], call: ToolCall) -> Mapping[str, Any]:
        return iam.grant_access(call.idempotency_key, args["email"], args["system"], args["level"])

    return {"iam.lookup_user": lookup, "iam.grant_access": grant}
