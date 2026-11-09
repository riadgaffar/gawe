"""What the API returns. These are views: they never include documents, prompts, signatures,
or anything the Reader saw."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MeView(View):
    user_id: str
    tenant_id: str
    roles: tuple[str, ...]


class StepView(View):
    step_id: str
    tool: str
    status: str
    depends_on: tuple[str, ...]
    policy_outcome: str | None = None
    policy_rule_ids: tuple[str, ...] = ()
    approver_role: str | None = None
    action_hash: str | None = None
    error: str | None = None


class RunView(View):
    run_id: str
    objective: str
    profile: str
    status: str
    error: str | None
    acting_user_id: str
    plan_version: int
    created_at: datetime
    completed_at: datetime | None
    steps: tuple[StepView, ...]


class ApprovalView(View):
    approval_id: str
    run_id: str
    step_id: str
    tool: str
    risk_tier: str
    resolved_args: dict[str, Any]
    action_hash: str
    approver_role: str
    reason: str
    requested_by: str
    requested_at: datetime
    expires_at: datetime
    decision: str | None = None
    decided_by: str | None = None


class AuditEventView(View):
    seq: int
    type: str
    actor: str
    payload_hash: str
    hash: str
    ts: datetime


class AnchorView(View):
    """How the chain compares with a copy of its head kept outside the database."""

    status: Literal["none", "matched", "mismatch", "evidence_invalid", "unavailable"]
    source: Literal["stored", "caller"] | None = None
    sink: str | None = None
    external: bool = False  # True only when an outside authority vouches for the anchor
    seq: int | None = None
    anchored_at: datetime | None = None
    timestamped_at: datetime | None = None
    events_after_anchor: int = 0


class AuditVerificationView(View):
    ok: bool
    checked: int
    head: str | None
    failed_seq: int | None
    reason: str | None
    anchor: AnchorView | None = None


class ToolView(View):
    name: str
    version: str
    description: str
    side_effect_class: str
    risk_tier: str
    required_roles: tuple[str, ...]
    input_schema: dict[str, Any]


class EventsPage(View):
    events: tuple[AuditEventView, ...]
    run_status: str
    finished: bool
