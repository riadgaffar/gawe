// Pure presentation of captured undo state. Only the API can authorize a reversal.
import type { StepView, UndoView } from "./api-types.ts";

const REASONS: Record<string, string> = {
  not_found: "This step was not found, or belongs to another tenant.",
  not_reversible: "This action cannot be undone.",
  undo_unavailable: "No confirmed undo record is available for this action.",
  already_undone: "Undo has already been requested for this action. Refresh its status.",
  undo_of_undo: "A restoration cannot itself be undone.",
  record_changed: "The contract changed after this rating was written. Undo was refused.",
  outcome_unknown: "The original action could not be confirmed, so undo is unavailable.",
  readback_mismatch: "The target's record did not match the action, so undo is unavailable.",
  result_version_unobserved: "The revision after the write could not be confirmed.",
  not_dispatched: "The original action was not sent; there is nothing to undo.",
  protocol_failure: "The original result could not be verified, so undo is unavailable.",
  forbidden: "Only a case manager in this tenant can request undo.",
  unreachable: "The request outcome is unknown. Refresh the status before trying again.",
  cancelled: "The undo run was cancelled.",
  denied: "Policy denied the restoration.",
  rejected: "The restoration was not approved.",
};
export function undoReason(code: string): string {
  return REASONS[code] ?? "Undo could not be completed. Check the undo run and refresh this status.";
}

function rating(value: unknown): string {
  return value === "none" ? "No rating" :
    value === "low" || value === "medium" || value === "high" ? value : "Unavailable";
}

export type UndoCardModel = {
  canUndo: boolean;
  status: string;
  writtenRating: string;
  priorRating: string;
  priorSummary: string | null;
  revisionNote: string;
  approvalNote: string;
  laterSteps: UndoView["later_steps"];
  effectsNote: string;
};

export function undoCardModel(
  step: StepView, view: UndoView | null, roles: readonly string[], profile: string,
): UndoCardModel {
  const record = view?.record;
  const expected = step.verification?.evidence.expected;
  const written = expected && typeof expected === "object" && "risk_level" in expected
    ? expected.risk_level : undefined;
  const matches = Boolean(record && record.step_id === step.step_id && record.action_hash === step.action_hash);
  let status = undoReason("undo_unavailable");
  if (profile === "undo" || step.tool === "crm.restore_assessment") status = undoReason("undo_of_undo");
  else if (step.tool !== "crm.write_assessment") status = undoReason("not_reversible");
  else if (!view) status = "Undo status could not be loaded. Refresh before requesting undo.";
  else if (matches && record) {
    switch (record.state) {
      case "armed": status = !record.result_version ? undoReason("undo_unavailable") : roles.includes("case_manager") ? "Available to request" : undoReason("forbidden"); break;
      case "pending": status = "Waiting for confirmation of the original write."; break;
      case "requested": status = "Undo has been requested. Its separate run may be waiting for approval."; break;
      case "used": status = "The previous rating was restored."; break;
      case "failed": status = undoReason(record.reason ?? "failed"); break;
      case "unavailable": status = undoReason(record.reason ?? "undo_unavailable"); break;
    }
  }
  return {
    canUndo: Boolean(matches && record?.state === "armed" && record.result_version &&
      roles.includes("case_manager") && profile !== "undo" && step.tool === "crm.write_assessment"),
    status,
    writtenRating: rating(written),
    priorRating: record && matches ? rating(record.prior?.risk_level ?? "none") : "Unavailable",
    priorSummary: record && matches && record.prior ? record.prior.summary : null,
    revisionNote: "Undo succeeds only if the contract hasn't changed since this rating was written. The check happens when the restoration is applied.",
    approvalNote: record?.prior === null || record?.prior?.risk_level === "low"
      ? "Restoring this rating requires legal approval. Requesting undo starts that review."
      : "Restoration goes through policy and may require approval.",
    laterSteps: view?.later_steps ?? [],
    effectsNote: "Effects of later steps stay as they are. Undo does not recall emails, signatures, or decisions already made.",
  };
}
