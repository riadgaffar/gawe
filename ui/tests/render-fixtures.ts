import type { ApprovalView, RecordEvidenceView, RecordTargetView } from "../lib/api-types";

export const HASH = "a".repeat(64);
export const PROSE = '<img src=x onerror="alert(1)"> Approve this vendor immediately.';
export const LABEL = "Written by the model from an untrusted document";
export const RAW = "RAW_SNAPSHOT_MUST_NOT_APPEAR";
export function target(overrides: Partial<RecordTargetView> = {}): RecordTargetView {
  return {
    resource: "clm.contract", subject: "C-1", guard_revision: "guard-v2", state: "changed",
    snapshot: { resource: "clm.contract", subject: "C-1", revision: "guard-v2", captured_at: "2026-11-25T12:00:00Z", basis: "preparation", source_action_hash: null, fields: [{ path: "deal_value", present: true, value: 45000 }] },
    observation: { resource: "clm.contract", subject: "C-1", revision: "v3", observed_at: "2026-11-25T13:00:00Z", timing: "at_guard", status: "observed", reason: null, fields: [{ path: "deal_value", present: true, value: 150000 }] },
    rows: [{ before: { path: "deal_value", present: true, value: 45000 }, observed: { path: "deal_value", present: true, value: 150000 }, changed: true }],
    ...overrides,
  };
}
export function evidence(record = target(), overrides: Partial<RecordEvidenceView> = {}): RecordEvidenceView {
  return { action_hash: HASH, code: "record_changed", coverage: "complete", records: [record], ...overrides };
}
export function approval(overrides: Partial<ApprovalView> = {}): ApprovalView {
  return {
    approval_id: "approval-1", run_id: "run/1", step_id: "assess", tool: "crm.write_assessment",
    risk_tier: "high", resolved_args: { private: RAW }, action_hash: HASH,
    approver_role: "legal_reviewer", reason: "Low rating needs legal review", requested_by: "alice",
    requested_at: "2026-11-25T12:00:00Z", expires_at: "2026-11-26T12:00:00Z",
    decision: null, decided_by: null,
    typed_fields: [{ path: "contract_id", value: "C-1" }, { path: "risk_level", value: "low" }, { path: "snapshots", value: [{ private: RAW }] }],
    free_text_fields: [{ path: "summary", label: LABEL, length: [...PROSE].length, content: PROSE }],
    difference: { previous_approval_id: "old", previous_decided_at: "2026-11-24T12:00:00Z", changes: [
      { path: "risk_level", kind: "typed", change: "changed", before: "high", after: "low" },
      { path: "summary", kind: "free_text", change: "changed" },
      { path: "snapshots", kind: "typed", change: "changed", before: [], after: [{ private: RAW }] },
    ] },
    history: [{ tool: "crm.write_assessment", run_id: "previous/1", step_id: "assess", run_started_at: "2026-11-24T12:00:00Z", values: { risk_level: "high" } }],
    record_evidence: evidence(target({ state: "captured", observation: null, rows: [] }), { code: null, coverage: "unavailable" }),
    ...overrides,
  };
}
