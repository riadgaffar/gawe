// Display only the API's validated, persisted projection; never read live target state.
import type { CapturedField, RecordEvidenceView, RecordTargetView, StepView } from "./api-types.ts";

export type RecordCardModel = {
  title: string; notice: string; coverage: string;
  records: {
    key: string; subject: string; resource: string; status: string;
    capture: string; timing: string;
    metadata: { label: string; value: string }[];
    rows: { field: string; before: string; observed: string; change: string }[];
  }[];
  refused: boolean;
};
const REFUSED = "This action was refused. It will not be retried or sent for another approval. Start a fresh run to reassess the action.";

export function isRevisionRefusal(code: string | null | undefined): boolean {
  return code === "record_changed" || code === "event_changed";
}
export function snapshotPath(path: string): boolean {
  return path === "snapshots" || path.startsWith("snapshots.");
}
export function fieldValue(field: CapturedField | undefined): string {
  if (!field) return "Unavailable";
  if (!field.present) return "Missing";
  return JSON.stringify(field.value) ?? "Unavailable";
}
function stateText(record: RecordTargetView): string {
  switch (record.state) {
    case "captured": return "Selected fields captured for this action.";
    case "changed": return "The observed selected fields differ from the captured values. The action remains refused.";
    case "unchanged": return record.observation?.timing === "after_refusal"
      ? "The selected fields match in this later read. Their values at refusal are not established. The action remains refused."
      : record.observation?.revision !== record.guard_revision
        ? "The record revision changed, but the selected fields did not. The action remains refused."
        : "The selected fields match even though the guard refused the action. Matching fields do not permit execution.";
    case "no_fields_tracked": return "No record fields are tracked for comparison. The revision guard still applies.";
    case "snapshot_unavailable": return "The captured snapshot is unavailable. No before/after comparison can be made.";
    case "observation_unavailable": return "The refusal observation is unavailable. This does not mean the record was unchanged.";
    case "record_missing": return "The record was missing or outside the permitted scope when observed.";
  }
}
export function recordCardModel(view: RecordEvidenceView | null | undefined, actionHash: string | null, refused = false, captureExpected = false): RecordCardModel | null {
  if (!view && !refused && !captureExpected) return null;
  const valid = view && view.action_hash === actionHash ? view : null;
  const isRefused = refused || Boolean(valid?.code);
  return {
    title: isRefused ? "Why the record check refused this action" : "Captured record fields",
    notice: isRefused ? REFUSED : "These saved values are part of the action you are approving.",
    coverage: !valid || valid.records.length === 0 ? "Saved record evidence is unavailable."
      : !isRefused ? ""
      : valid.coverage === "partial" ? "Only part of the batch was observed. Targets without observations cannot be compared."
      : valid.coverage === "complete" ? "Every target has an observation; individual records may still be missing or unreadable."
      : "Refusal observations are unavailable.",
    refused: isRefused,
    records: (valid?.records ?? []).map(record => {
      const before = record.snapshot, observed = record.observation;
      const paths = [...new Set([...(before?.fields ?? []).map(f => f.path), ...(observed?.fields ?? []).map(f => f.path)])];
      return {
        key: `${record.resource}/${record.subject}`, subject: record.subject, resource: record.resource,
        status: stateText(record),
        capture: before ? `${before.basis === "confirmed_original" ? "Captured for the original confirmed action" : "Captured for this action"} at ${before.captured_at}.` : "No saved capture is available.",
        timing: !observed ? (isRefused ? "No saved observation is available for this target." : "")
          : observed.timing === "at_guard" ? `Observed by the atomic guard at ${observed.observed_at}.`
          : `Read after refusal at ${observed.observed_at}. This is not the historical guard state.`,
        metadata: [
          { label: "Required revision for this action", value: record.guard_revision },
          ...(before ? [{ label: "Captured revision", value: before.revision }] : []),
          ...(before?.source_action_hash ? [{ label: "Original action hash", value: before.source_action_hash }] : []),
          ...(observed?.revision ? [{ label: "Observed revision", value: observed.revision }] : []),
        ],
        rows: paths.map(path => ({
          field: path, before: fieldValue(before?.fields.find(f => f.path === path)),
          observed: observed?.status === "missing" ? "Record missing" : fieldValue(observed?.fields.find(f => f.path === path)),
          change: record.rows.find(row => row.before.path === path)?.changed === true ? "Changed"
            : record.rows.some(row => row.before.path === path) ? "Unchanged" : "Not compared",
        })),
      };
    }),
  };
}
export function refusalCardModel(step: StepView): RecordCardModel | null {
  if (!isRevisionRefusal(step.error) && !isRevisionRefusal(step.verification?.code)) return null;
  return recordCardModel(step.record_evidence, step.action_hash, true);
}
