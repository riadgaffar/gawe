"""Interface contracts shared by the engine, API and apps.

All models are frozen and reject unknown fields: anything crossing a trust boundary
(especially LLM-produced plans) must match these shapes exactly.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SEMVER_PATTERN = r"^\d+\.\d+\.\d+$"
TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_.]{0,127}$"
STEP_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

REF_KEY = "$ref"

SideEffectClass = Literal["read", "write", "external"]
RiskTier = Literal["low", "medium", "high"]
PolicyOutcome = Literal["allow", "needs_approval", "deny"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolManifest(FrozenModel):
    """Machine-readable description of a tool. The policy engine classifies actions from this."""

    name: str = Field(pattern=TOOL_NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    description: str = ""
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    side_effect_class: SideEffectClass
    risk_tier: RiskTier
    required_roles: tuple[str, ...] = ()
    postcondition: str | None = None
    # None is undeclared; () explicitly declares no application-resource writes.
    writes: tuple[str, ...] | None = None


class PlanStep(FrozenModel):
    """One proposed action. `args` values may be literals or a top-level {"$ref": ...} reference
    to an earlier step's output, formatted as "<step_id>.output[.path...]"."""

    id: str = Field(pattern=STEP_ID_PATTERN)
    tool: str = Field(pattern=TOOL_NAME_PATTERN)
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()


class Plan(FrozenModel):
    steps: tuple[PlanStep, ...]


class PolicyDecision(FrozenModel):
    step_id: str
    action_hash: str
    outcome: PolicyOutcome
    rule_ids: tuple[str, ...]
    reason: str
    approver_role: str | None = None
    signature: str


class ApprovalRequest(FrozenModel):
    approval_id: str
    step_id: str
    action_hash: str
    tool: str
    resolved_args: dict[str, Any]
    risk_tier: RiskTier
    requested_by: str
    expires_at: datetime


class AuditEvent(FrozenModel):
    seq: int
    run_id: str
    type: str
    actor: str
    payload_hash: str
    prev_hash: str
    hash: str
    ts: datetime


def is_ref(value: Any) -> bool:
    """True if `value` is exactly {"$ref": "<string>"}."""
    return isinstance(value, dict) and set(value) == {REF_KEY} and isinstance(value[REF_KEY], str)


def has_ref_key(value: Any) -> bool:
    """True if `$ref` appears as a key anywhere inside `value` (used to catch smuggled refs)."""
    if isinstance(value, dict):
        return REF_KEY in value or any(has_ref_key(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_ref_key(v) for v in value)
    return False
