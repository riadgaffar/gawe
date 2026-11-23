// Mirrors src/gawe/api/schemas.py. Keep in sync with docs/api.md.

export type VerificationView = {
  job_id: string;
  step_id: string;
  plan_version: number;
  operation_key: string;
  action_hash: string;
  deadline: string;
  due_at: string;
  outcome: string | null;
  code: string | null;
  protocol_error: string | null;
  evidence: Record<string, unknown>;
};

export type CheckView = {
  rule_id: string;
  path: string;
  op: string;
  unreadable: boolean;
};

export type StepView = {
  verification?: VerificationView | null;
  step_id: string;
  tool: string;
  status: string;
  depends_on: string[];
  policy_outcome: string | null;
  policy_rule_ids: string[];
  policy_checks: CheckView[];
  approver_role: string | null;
  action_hash: string | null;
  error: string | null;
  handoff?: HandoffMarkView | null;
  // M22: conditions read just before sending, which the target can't enforce.
  checked_not_enforced?: string[];
};

// A prepare-only step (M20): the role that may mark it, and what the person said.
export type HandoffMarkView = {
  role: string;
  marked_by: string | null;
  marked_at: string | null;
  reference: string | null;
};

export type RunView = {
  run_id: string;
  objective: string;
  profile: string;
  source_id: string | null;
  status: string;
  error: string | null;
  acting_user_id: string;
  plan_version: number;
  created_at: string;
  completed_at: string | null;
  steps: StepView[];
};

export type ApprovalView = {
  approval_id: string;
  run_id: string;
  step_id: string;
  tool: string;
  risk_tier: string;
  resolved_args: Record<string, unknown>;
  action_hash: string;
  approver_role: string;
  reason: string;
  requested_by: string;
  requested_at: string;
  expires_at: string;
  decision: string | null;
  decided_by: string | null;
  typed_fields: TypedFieldView[];
  free_text_fields: FreeTextFieldView[];
  difference: DifferenceView;
};

// A prepare-only action waiting for a person to carry it out by hand (M20).
export type HandoffView = {
  run_id: string;
  step_id: string;
  tool: string;
  risk_tier: string;
  resolved_args: Record<string, unknown>;
  preparation: Record<string, unknown>;
  action_hash: string;
  role: string;
  policy_outcome: string | null;
  reason: string;
  requested_by: string;
  status: string;
  marked_by: string | null;
  marked_at: string | null;
  reference: string | null;
  typed_fields: TypedFieldView[];
  free_text_fields: FreeTextFieldView[];
};

export type HandoffInput = {
  outcome: "done" | "not_done";
  action_hash: string;
  reference?: string;
};

export type TypedFieldView = {
  path: string;
  value: unknown;
};

// Model-written text: show path, label and length; render content only on explicit expand.
export type FreeTextFieldView = {
  path: string;
  label: string;
  length: number;
  content: unknown;
};

export type ChangeView = {
  path: string;
  kind: "typed" | "free_text";
  change: "added" | "removed" | "changed" | "unchanged";
  before?: unknown;
  after?: unknown;
};

export type DifferenceView = {
  previous_approval_id: string | null;
  previous_decided_at: string | null;
  changes: ChangeView[];
};

export type AuditEventView = {
  seq: number;
  type: string;
  actor: string;
  payload_hash: string;
  hash: string;
  ts: string;
};

export type AnchorView = {
  status: "none" | "matched" | "mismatch" | "evidence_invalid" | "unavailable";
  source: "stored" | "caller" | null;
  sink: string | null;
  external: boolean;
  seq: number | null;
  anchored_at: string | null;
  timestamped_at: string | null;
  events_after_anchor: number;
};

export type AuditVerificationView = {
  ok: boolean;
  checked: number;
  head: string | null;
  failed_seq: number | null;
  reason: string | null;
  anchor?: AnchorView | null;
};

export type MeView = { user_id: string; tenant_id: string; roles: string[] };

// IT access names a ticket (`source_id`) instead of sending a document; the server reads it.
export type StartRunInput = { objective: string; profile: string; document?: string; source_id?: string };

// A next-day review finding or sample: ids and a reason, never argument values.
export type ReviewItemView = {
  item_id: string;
  day: string;
  run_id: string;
  step_id: string | null;
  action_hash: string | null;
  reason: string;
  key_id: string | null;
  status: "open" | "confirmed" | "missed_risk";
  created_at: string;
  decided_by: string | null;
  decided_at: string | null;
};

export type Verdict = "confirmed" | "missed_risk";

export type DecisionInput = {
  decision: "approved" | "rejected";
  action_hash: string;
  comment?: string;
};
