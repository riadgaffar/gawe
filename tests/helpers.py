"""Shared test builders (kept out of conftest so they can be imported explicitly)."""

from collections.abc import Mapping, Sequence
from typing import Any

from gawe.core.models import ToolManifest
from gawe.core.policy.rules import EvaluationContext
from gawe.core.policy.signing import PolicySigner

KEY = bytes(range(32))
DEFAULT_VARIABLES: dict[str, Any] = {"tenant": {"internal_domains": ["acme.com"]}}


def signer() -> PolicySigner:
    return PolicySigner(KEY)


def tool(
    name: str,
    side_effect: str,
    risk: str,
    properties: dict[str, Any],
    required: Sequence[str] = (),
    roles: Sequence[str] = (),
) -> ToolManifest:
    return ToolManifest(
        name=name,
        version="1.0.0",
        input_schema={
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effect_class=side_effect,  # type: ignore[arg-type]
        risk_tier=risk,  # type: ignore[arg-type]
        required_roles=tuple(roles),
    )


TOOLS: dict[str, ToolManifest] = {
    "contracts.extract_risks": tool(
        "contracts.extract_risks",
        "read",
        "low",
        {"document_id": {"type": "string"}},
        ["document_id"],
    ),
    "contracts.send_email": tool(
        "contracts.send_email",
        "external",
        "high",
        {"to": {"type": "string"}, "body": {"type": "string"}},
        ["to", "body"],
        roles=["case_manager", "legal_reviewer"],
    ),
    "crm.write_assessment": tool(
        "crm.write_assessment",
        "write",
        "medium",
        {"contract_id": {"type": "string"}, "risk_score": {"type": "integer"}},
        ["contract_id", "risk_score"],
    ),
    "crm.mark_approved_for_signature": tool(
        "crm.mark_approved_for_signature",
        "write",
        "high",
        {"contract_id": {"type": "string"}, "deal_value": {"type": "number", "minimum": 0}},
        ["contract_id", "deal_value"],
        roles=["case_manager"],
    ),
    "esign.send_for_signature": tool(
        "esign.send_for_signature",
        "external",
        "high",
        {"contract_id": {"type": "string"}},
        ["contract_id"],
    ),
}


def make_ctx(
    manifest: ToolManifest,
    args: Mapping[str, Any],
    *,
    roles: Sequence[str] = (),
    variables: Mapping[str, Any] | None = None,
    run_id: str = "run1",
    step_id: str = "s1",
    user: str = "u1",
) -> EvaluationContext:
    return EvaluationContext(
        run_id=run_id,
        step_id=step_id,
        manifest=manifest,
        resolved_args=dict(args),
        acting_user_id=user,
        user_roles=frozenset(roles),
        variables=DEFAULT_VARIABLES if variables is None else variables,
    )
