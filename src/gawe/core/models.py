"""Interface contracts shared by the engine, API and apps.

All models are frozen and reject unknown fields: anything crossing a trust boundary
(especially LLM-produced plans) must match these shapes exactly.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

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
    proposal_schema: dict[str, Any] | None = None
    preparation_required: bool = False
    side_effect_class: SideEffectClass
    risk_tier: RiskTier
    required_roles: tuple[str, ...] = ()
    postcondition: str | None = None
    # None is undeclared; () explicitly declares no application-resource writes.
    writes: tuple[str, ...] | None = None
    # The system-of-record resource whose revision preparation binds into the arguments
    # (for example "clm.contract"). Such a tool needs a preparer and a conditional write.
    record_binding: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.]{0,127}$")
    # For every leaf path of input_schema: "typed" (shown first on the approval card) or
    # "free_text" (model-written prose, collapsed). Registration requires it to be complete.
    field_classes: dict[str, Literal["typed", "free_text"]] | None = None
    # Kept registered only so writes dispatched before retirement can still be verified;
    # a step pinned to a retired version that has not dispatched fails closed.
    retired: bool = False
    # Only trusted code may construct plans naming this tool.
    trusted_only: bool = False


class PlanStep(FrozenModel):
    """One proposed action. `args` values may be literals or a top-level {"$ref": ...} reference
    to an earlier step's output, formatted as "<step_id>.output[.path...]"."""

    id: str = Field(pattern=STEP_ID_PATTERN)
    tool: str = Field(pattern=TOOL_NAME_PATTERN)
    # Persisted trusted plans may pin a version. Model-plan validation rejects a supplied pin.
    tool_version: str | None = Field(default=None, pattern=SEMVER_PATTERN)
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()

    @model_serializer(mode="wrap")
    def _omit_absent_pin(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Preserve the serialized shape of pre-M27/model plans and their audit payloads.
        data: dict[str, Any] = handler(self)
        if self.tool_version is None:
            data.pop("tool_version", None)
        return data


class Plan(FrozenModel):
    steps: tuple[PlanStep, ...]


class DecisionCheck(FrozenModel):
    """One evaluated rule check. Never carries the argument value or the operand."""

    rule_id: str
    path: str
    op: str
    unreadable: bool


class PolicyDecision(FrozenModel):
    """A signed policy outcome.

    Format 1 (before M16b) has no checks and no rule-set digest. Format 2 adds the ordered list of
    evaluated checks with a per-check unreadable marker, and the digest of the loaded rule set.
    JSON without `format` parses as format 1; a format 1 decision carrying the new fields is
    invalid, so they cannot be attached to an old signature.
    """

    step_id: str
    action_hash: str
    outcome: PolicyOutcome
    rule_ids: tuple[str, ...]
    reason: str
    approver_role: str | None = None
    signature: str
    format: Literal[1, 2] = 1
    checks: tuple[DecisionCheck, ...] = ()
    policy_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _check_format(self) -> "PolicyDecision":
        if self.format == 1 and (self.checks or self.policy_digest is not None):
            raise ValueError("a format 1 decision has no checks and no policy digest")
        if self.format == 2 and self.policy_digest is None:
            raise ValueError("a format 2 decision needs a policy digest")
        return self


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
