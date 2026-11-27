// Mirrors src/gawe/api/schemas.py. Keep in sync with docs/api.md.

export type VerificationView = {
  record_evidence?: RecordEvidenceView | null;
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
  record_evidence?: RecordEvidenceView | null;
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
  // M29: set only for a run under the scripted demo model.
  model_mode?: "honest" | "fooled" | null;
  status: string;
  error: string | null;
  acting_user_id: string;
  plan_version: number;
  created_at: string;
  completed_at: string | null;
  steps: StepView[];
};

export type ApprovalView = {
  record_evidence?: RecordEvidenceView | null;
  model_mode?: "honest" | "fooled" | null;
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
  history: HistoryItemView[];
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

// An earlier action on the same subject that a rule counted (M24): typed values only.
export type HistoryItemView = {
  tool: string;
  run_id: string;
  step_id: string;
  run_started_at: string;
  values: Record<string, unknown>;
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
export type StartRunInput = {
  objective: string;
  profile: string;
  document?: string;
  source_id?: string;
  model_mode?: "honest" | "fooled";
};

// M29: what GET /v1/demo tells the UI. Names, roles and scenes; never a token.
export type DemoPerson = { key: string; name: string; title: string; roles: string[] };
export type DemoCompany = {
  key: string;
  name: string;
  tenant_id: string;
  summary: string;
  people: DemoPerson[];
};
export type DemoScene = {
  scene_id: string;
  title: string;
  objective: string;
  document: string;
  modes: ("honest" | "fooled")[];
  fooled_how: string | null;
  expect: string;
};
export type DemoInfo = {
  model: string;
  companies: DemoCompany[];
  scenes: DemoScene[];
  fixture: { company: string; run_id: string } | null;
};

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


// GET /v1/runs/{run}/steps/{step}/undo. The record is captured state, not a live CRM read.
export type UndoRecordView = {
  undo_id: string;
  run_id: string;
  step_id: string;
  action_hash: string;
  state: "pending" | "armed" | "unavailable" | "requested" | "used" | "failed";
  prior: { risk_level: string; summary: string } | null;
  result_version: string | null;
  reason: string | null;
};
export type UndoView = {
  record: UndoRecordView | null;
  later_steps: { step_id: string; tool: string; outcome: string }[];
  notice: string;
};

export type CapturedField = { path: string; present: boolean; value: string | number | boolean | null };
export type RecordSnapshot = {
  resource: string; subject: string; revision: string; captured_at: string;
  basis: "preparation" | "confirmed_original"; source_action_hash: string | null;
  fields: CapturedField[];
};
export type RecordObservation = {
  resource: string; subject: string; revision: string | null; observed_at: string;
  timing: "at_guard" | "after_refusal"; status: "observed" | "missing" | "unavailable";
  reason: "unreadable" | "not_recorded" | "invalid" | null; fields: CapturedField[];
};
export type RecordTargetView = {
  resource: string; subject: string; guard_revision: string;
  state: "captured" | "changed" | "unchanged" | "no_fields_tracked" | "snapshot_unavailable" | "observation_unavailable" | "record_missing";
  snapshot: RecordSnapshot | null; observation: RecordObservation | null;
  rows: { before: CapturedField; observed: CapturedField; changed: boolean }[];
};
export type RecordEvidenceView = {
  action_hash: string; code: "record_changed" | "event_changed" | null;
  coverage: "complete" | "partial" | "unavailable"; records: RecordTargetView[];
};
