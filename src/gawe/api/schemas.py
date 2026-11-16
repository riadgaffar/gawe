"""What the API returns. These are views: they never include documents, prompts, signatures,
or anything the Reader saw."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MeView(View):
    user_id: str
    tenant_id: str
    roles: tuple[str, ...]


class VerificationView(View):
    job_id: str
    step_id: str
    plan_version: int
    operation_key: str
    action_hash: str
    deadline: datetime
    due_at: datetime
    outcome: str | None = None
    code: str | None = None
    protocol_error: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class CheckView(View):
    """One evaluated policy check: never the argument value or the operand."""

    rule_id: str
    path: str
    op: str
    unreadable: bool


class StepView(View):
    step_id: str
    tool: str
    status: str
    depends_on: tuple[str, ...]
    policy_outcome: str | None = None
    policy_rule_ids: tuple[str, ...] = ()
    policy_checks: tuple[CheckView, ...] = ()
    approver_role: str | None = None
    action_hash: str | None = None
    error: str | None = None
    verification: VerificationView | None = None


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


class TypedFieldView(View):
    path: str
    value: Any


class FreeTextFieldView(View):
    """Model-written text. Clients show the header (path, label, length) and render `content`
    only after an explicit expand."""

    path: str
    label: str
    length: int
    content: Any


class ChangeView(View):
    """One difference from the last approved call. Free text has no before or after."""

    path: str
    kind: Literal["typed", "free_text"]
    change: Literal["added", "removed", "changed", "unchanged"]
    before: Any = None
    after: Any = None


class DifferenceView(View):
    """Against the most recent approved approval for the same tool in the same tenant."""

    previous_approval_id: str | None = None
    previous_decided_at: datetime | None = None
    changes: tuple[ChangeView, ...] = ()


class ApprovalView(View):
    preparation: dict[str, Any] = Field(default_factory=dict)
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
    typed_fields: tuple[TypedFieldView, ...] = ()
    free_text_fields: tuple[FreeTextFieldView, ...] = ()
    difference: DifferenceView = Field(default_factory=DifferenceView)


class ReviewItemView(View):
    """A next-day review finding or sample. Carries ids and reasons, never argument values."""

    item_id: str
    day: date
    run_id: str
    step_id: str | None
    action_hash: str | None
    reason: str
    key_id: str | None
    status: str
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None


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
