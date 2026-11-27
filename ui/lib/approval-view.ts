// The approval card's model, built from the server's classification. Pure, so it runs under the
// plain Node test runner. Typed fields come first; model-written text is collapsed behind a
// header that carries only its path, a label and its length, never any of its text.
import type { ApprovalView, ChangeView, HistoryItemView } from "./api-types.ts";

import { recordCardModel, snapshotPath, type RecordCardModel } from "./record-evidence.ts";

export type Table = { columns: string[]; rows: string[][] };
export type TypedRow = { path: string; display: string; table?: Table };
export type FreeTextSection = { path: string; header: string; content: string };
export type DifferenceModel = { summary: string; rows: string[] };
export type HistoryRow = { key: string; runId: string; text: string };
export type ApprovalCardModel = {
  typed: TypedRow[];
  records: RecordCardModel | null;
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

// A typed list of flat records (a replay batch, say) as a table: one row per record, one column
// per field, in first-seen order. Anything else, including nested values, stays as `display`.
export function tableOf(value: unknown): Table | undefined {
  if (!Array.isArray(value) || value.length === 0) return undefined;
  const isRecord = (item: unknown): item is Record<string, unknown> =>
    typeof item === "object" && item !== null && !Array.isArray(item);
  if (!value.every(isRecord)) return undefined;
  const flat = value.every((item) =>
    Object.values(item).every((v) => v === null || typeof v !== "object"),
  );
  if (!flat) return undefined;
  const columns: string[] = [];
  for (const item of value) for (const key of Object.keys(item)) if (!columns.includes(key)) columns.push(key);
  const rows = value.map((item) => columns.map((c) => (c in item ? display(item[c]) : "")));
  return { columns, rows };
}

function typedRow(path: string, value: unknown): TypedRow {
  const table = tableOf(value);
  return table ? { path, display: display(value), table } : { path, display: display(value) };
}

export function freeTextHeader(label: string, length: number): string {
  return `${label} · ${length} ${length === 1 ? "character" : "characters"}`;
}

function changeRow(change: ChangeView): string {
  if (snapshotPath(change.path)) return `${change.path}: ${change.change} (captured record evidence; see captured fields)`;
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
  const changes = difference.changes;
  const meaningful = changes.filter((c) => c.change !== "unchanged");
  return {
    summary:
      meaningful.length === 0
        ? "Same as the last approved call"
        : `${meaningful.length} ${meaningful.length === 1 ? "difference" : "differences"} from the last approved call`,
    rows: changes.map(changeRow),
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
    records: recordCardModel(approval.record_evidence, approval.action_hash, false, approval.typed_fields.some(field => snapshotPath(field.path))),
    typed: approval.typed_fields.filter(field => !snapshotPath(field.path)).map((field) => typedRow(field.path, field.value)),
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
