import assert from "node:assert/strict";
import { test } from "node:test";
import type { RecordEvidenceView, RecordTargetView, StepView } from "../lib/api-types.ts";
import { fieldValue, recordCardModel, refusalCardModel } from "../lib/record-evidence.ts";

const HASH = "a".repeat(64);
function target(overrides: Partial<RecordTargetView> = {}): RecordTargetView {
  return {
    resource: "clm.contract", subject: "C-1", guard_revision: "guard-v2", state: "changed",
    snapshot: { resource: "clm.contract", subject: "C-1", revision: "guard-v2", captured_at: "2026-11-25T12:00:00Z", basis: "preparation", source_action_hash: null, fields: [{path: "deal_value", present: true, value: 45000}] },
    observation: { resource: "clm.contract", subject: "C-1", revision: "v3", observed_at: "2026-11-25T13:00:00Z", timing: "at_guard", status: "observed", reason: null, fields: [{path: "deal_value", present: true, value: 150000}] },
    rows: [{before: {path: "deal_value", present: true, value: 45000}, observed: {path: "deal_value", present: true, value: 150000}, changed: true}],
    ...overrides,
  };
}
function evidence(record = target(), overrides: Partial<RecordEvidenceView> = {}): RecordEvidenceView {
  return { action_hash: HASH, code: "record_changed", coverage: "complete", records: [record], ...overrides };
}

test("approval capture shows selected fields without implying an observation", () => {
  const card = recordCardModel(evidence(target({state: "captured", observation: null, rows: []}), {code: null, coverage: "unavailable"}), HASH)!;
  assert.equal(card.refused, false);
  assert.match(card.notice, /part of the action you are approving/);
  assert.equal(card.records[0]?.timing, "");
  assert.equal(card.records[0]?.rows[0]?.before, "45000");
});

test("guard change shows before, observed and a continuing refusal", () => {
  const card = recordCardModel(evidence(), HASH)!;
  assert.equal(card.refused, true);
  assert.match(card.notice, /will not be retried or sent for another approval/);
  assert.match(card.records[0]!.timing, /atomic guard/);
  assert.deepEqual(card.records[0]!.rows, [{field: "deal_value", before: "45000", observed: "150000", change: "Changed"}]);
});

test("matching fields never permit a revision-refused action", () => {
  const record = target({state: "unchanged"});
  record.observation!.fields[0]!.value = 45000;
  record.rows[0]!.changed = false;
  record.rows[0]!.observed.value = 45000;
  const card = recordCardModel(evidence(record), HASH)!;
  assert.match(card.records[0]!.status, /revision changed, but the selected fields did not/);
  assert.match(card.records[0]!.status, /remains refused/);
  assert.equal(card.records[0]!.rows[0]!.change, "Unchanged");
});

test("a later read is never described as guard history", () => {
  const record = target({state: "unchanged"});
  record.observation!.timing = "after_refusal";
  const card = recordCardModel(evidence(record), HASH)!;
  assert.match(card.records[0]!.timing, /Read after refusal.*not the historical guard state/);
  assert.match(card.records[0]!.status, /values at refusal are not established/);
});

for (const [state, text] of [
  ["no_fields_tracked", "No record fields are tracked"],
  ["snapshot_unavailable", "captured snapshot is unavailable"],
  ["observation_unavailable", "does not mean the record was unchanged"],
  ["record_missing", "missing or outside the permitted scope"],
] as const) {
  test(`explicit ${state} state`, () => {
    const record = target({state, rows: []});
    if (state === "snapshot_unavailable") record.snapshot = null;
    if (state === "observation_unavailable") record.observation = null;
    if (state === "no_fields_tracked") { record.snapshot!.fields = []; record.observation!.fields = []; }
    if (state === "record_missing") { record.observation!.status = "missing"; record.observation!.revision = null; record.observation!.fields = []; }
    const card = recordCardModel(evidence(record), HASH)!;
    assert.ok(card.records[0]!.status.includes(text));
    assert.ok(card.records[0]!.rows.every(row => row.change === "Not compared"));
    if (state === "record_missing") assert.equal(card.records[0]!.rows[0]!.observed, "Record missing");
  });
}

test("partial coverage retains unobserved targets and says they cannot be compared", () => {
  const view = evidence(target(), {coverage: "partial"});
  view.records.push(target({subject: "C-2", observation: null, state: "observation_unavailable", rows: []}));
  const card = recordCardModel(view, HASH)!;
  assert.match(card.coverage, /Only part of the batch/);
  assert.equal(card.records.length, 2);
  assert.match(card.records[1]!.timing, /No saved observation/);
  assert.equal(card.records[1]!.rows[0]!.observed, "Unavailable");
});

test("original undo provenance and the different guard revision stay distinct", () => {
  const record = target();
  record.snapshot!.basis = "confirmed_original";
  record.snapshot!.revision = "original-v1";
  record.snapshot!.source_action_hash = "b".repeat(64);
  const card = recordCardModel(evidence(record), HASH)!;
  assert.match(card.records[0]!.capture, /original confirmed action/);
  assert.deepEqual(card.records[0]!.metadata.slice(0, 3), [
    {label: "Required revision for this action", value: "guard-v2"},
    {label: "Captured revision", value: "original-v1"},
    {label: "Original action hash", value: "b".repeat(64)},
  ]);
});

test("missing, null, strings and unavailable have distinct displays", () => {
  assert.equal(fieldValue(undefined), "Unavailable");
  assert.equal(fieldValue({path: "x", present: false, value: null}), "Missing");
  assert.equal(fieldValue({path: "x", present: true, value: null}), "null");
  assert.equal(fieldValue({path: "x", present: true, value: "null"}), '\"null\"');
  assert.equal(fieldValue({path: "x", present: true, value: false}), "false");
});

test("absent or mismatched evidence never means unchanged, including old API responses", () => {
  const card = recordCardModel(evidence(), "wrong", true)!;
  assert.deepEqual(card.records, []);
  assert.match(card.coverage, /unavailable/);
  assert.equal(recordCardModel(null, HASH), null);
  const step = {error: "event_changed", action_hash: HASH} as StepView;
  assert.match(refusalCardModel(step)!.coverage, /unavailable/);
  assert.equal(refusalCardModel({...step, error: "other"}), null);
});

