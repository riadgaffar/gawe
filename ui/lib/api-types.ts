// Mirrors src/gawe/api/schemas.py. Keep in sync with docs/api.md.

export type StepView = {
  step_id: string;
  tool: string;
  status: string;
  depends_on: string[];
  policy_outcome: string | null;
  policy_rule_ids: string[];
  approver_role: string | null;
  action_hash: string | null;
  error: string | null;
};

export type RunView = {
  run_id: string;
  objective: string;
  profile: string;
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

export type StartRunInput = { objective: string; profile: string; document?: string };

export type DecisionInput = {
  decision: "approved" | "rejected";
  action_hash: string;
  comment?: string;
};
