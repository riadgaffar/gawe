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


class HandoffMarkView(View):
    """Who may mark a prepare-only step, and what the person said (M20)."""

    role: str
    marked_by: str | None = None
    marked_at: datetime | None = None
    reference: str | None = None


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
    handoff: HandoffMarkView | None = None
    # Conditions in other systems this write relies on that its target can't enforce: read just
    # before sending, with a window left between that read and the write (M22).
    checked_not_enforced: tuple[str, ...] = ()


class RunView(View):
    run_id: str
    objective: str
    profile: str
    source_id: str | None = None
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


class HandoffView(View):
    """A prepare-only action waiting for a person to carry it out by hand (M20). The same
    typed-first layout as an approval; free text is collapsed."""

    run_id: str
    step_id: str
    tool: str
    risk_tier: str
    resolved_args: dict[str, Any]
    preparation: dict[str, Any] = Field(default_factory=dict)
    action_hash: str
    role: str
    policy_outcome: str | None = None
    reason: str
    requested_by: str
    status: str
    marked_by: str | None = None
    marked_at: datetime | None = None
    reference: str | None = None
    typed_fields: tuple[TypedFieldView, ...] = ()
    free_text_fields: tuple[FreeTextFieldView, ...] = ()


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


# --- step trace (M23) ------------------------------------------------------------------------
# Hashes, ids, codes and times only, like the audit trail: never argument values, model text or
# read-back evidence.


class TraceDecisionView(View):
    outcome: str
    rule_ids: tuple[str, ...]
    approver_role: str | None = None
    action_hash: str
    signature_valid: bool | None  # None: this service has no key to check it with
    audited: bool  # its `policy.decided` event is in the run's audit trail


class TraceApprovalView(View):
    approval_id: str
    action_hash: str
    current: bool  # bound to the step's current action hash
    approver_role: str
    requested_by: str
    requested_at: datetime
    decision: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    audited: bool  # its request and (a person's) decision are in the audit trail


class TraceAttemptView(View):
    attempt: int
    status: str
    operation_key: str
    action_hash: str | None  # the hash the key was derived from; None when it matches none
    error: str | None = None
    output_hash: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class TraceConditionView(View):
    result: str  # "held", "changed:<condition>" or "unreadable:<condition>"
    count: int


class TraceReadbackView(View):
    job_id: str
    operation_key: str
    plan_version: int
    action_hash: str
    current: bool
    outcome: str | None = None
    code: str | None = None


class StepTraceView(View):
    """One step's records joined by the hash or key that links them, and the links that fail."""

    run_id: str
    step_id: str
    plan_version: int
    tool: str
    tool_version: str
    status: str
    error: str | None = None
    args_hash: str | None  # hash of the resolved arguments, never the arguments
    action_hash: str | None
    superseded_hashes: tuple[str, ...] = ()  # earlier hashes a recipient change replaced
    decision: TraceDecisionView | None = None
    approvals: tuple[TraceApprovalView, ...] = ()
    attempts: tuple[TraceAttemptView, ...] = ()
    conditions: tuple[TraceConditionView, ...] = ()
    handoff: HandoffMarkView | None = None
    readback: tuple[TraceReadbackView, ...] = ()
    gaps: tuple[str, ...] = ()
