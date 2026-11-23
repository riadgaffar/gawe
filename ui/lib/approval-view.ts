// The approval card's model, built from the server's classification. Pure, so it runs under the
// plain Node test runner. Typed fields come first; model-written text is collapsed behind a
// header that carries only its path, a label and its length, never any of its text.
import type { ApprovalView, ChangeView, HistoryItemView } from "./api-types.ts";

export type TypedRow = { path: string; display: string };
export type FreeTextSection = { path: string; header: string; content: string };
export type DifferenceModel = { summary: string; rows: string[] };
export type HistoryRow = { key: string; runId: string; text: string };
export type ApprovalCardModel = {
  typed: TypedRow[];
  history: HistoryRow[];
  actionHash: string;
  difference: DifferenceModel;
  freeText: FreeTextSection[];
};

export function display(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value) && value.every((item) => typeof item === "string")) {
    return value.join(", ");
  }
  return JSON.stringify(value) ?? "null";
}

export function freeTextHeader(label: string, length: number): string {
  return `${label} · ${length} ${length === 1 ? "character" : "characters"}`;
}

function changeRow(change: ChangeView): string {
  if (change.kind === "free_text") {
    return `${change.path}: ${change.change} (model-written text, not shown)`;
  }
  if (change.change === "added") return `${change.path}: added ${display(change.after)}`;
  if (change.change === "removed") return `${change.path}: removed (was ${display(change.before)})`;
  return `${change.path}: ${display(change.before)} → ${display(change.after)}`;
}

export function differenceModel(approval: ApprovalView): DifferenceModel {
  const { difference } = approval;
  if (!difference.previous_approval_id) {
    return { summary: "No earlier approved call", rows: [] };
  }
  const meaningful = difference.changes.filter((c) => c.change !== "unchanged");
  return {
    summary:
      meaningful.length === 0
        ? "Same as the last approved call"
        : `${meaningful.length} ${meaningful.length === 1 ? "difference" : "differences"} from the last approved call`,
    rows: difference.changes.map(changeRow),
  };
}

// One earlier action a rule counted, oldest first: its typed values, then the day its run started.
export function historyRow(item: HistoryItemView): HistoryRow {
  const values = Object.entries(item.values).map(([name, value]) => `${name} ${display(value)}`);
  return {
    key: `${item.run_id}/${item.step_id}`,
    runId: item.run_id,
    text: `${[item.tool, ...values].join(" · ")} · run started ${item.run_started_at.slice(0, 10)}`,
  };
}

export function approvalCardModel(approval: ApprovalView): ApprovalCardModel {
  return {
    typed: approval.typed_fields.map((field) => ({ path: field.path, display: display(field.value) })),
    history: (approval.history ?? []).map(historyRow),
    actionHash: approval.action_hash,
    difference: differenceModel(approval),
    freeText: approval.free_text_fields.map((field) => ({
      path: field.path,
      header: freeTextHeader(field.label, field.length),
      content: display(field.content),
    })),
  };
}
