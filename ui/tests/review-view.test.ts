import assert from "node:assert/strict";
import { test } from "node:test";
import type { ReviewItemView } from "../lib/api-types.ts";
import { isSample, reasonLabel } from "../lib/review-view.ts";

function item(reason: string): ReviewItemView {
  return {
    item_id: "i".repeat(64),
    day: "2026-11-15",
    run_id: "r1",
    step_id: "rate",
    action_hash: "a".repeat(64),
    reason,
    key_id: reason === "sampled_low" ? "0123456789abcdef" : null,
    status: "open",
    created_at: "2026-11-16T01:00:00Z",
    decided_by: null,
    decided_at: null,
  };
}

test("every reason the API can return has a plain label", () => {
  for (const reason of [
    "audit_mismatch",
    "superseded",
    "signature_invalid",
    "action_mismatch",
    "rules_changed",
    "rules_may_have_changed",
    "inputs_changed",
    "sampled_low",
  ]) {
    assert.ok(!reasonLabel(reason).includes("_"), reason);
  }
  assert.equal(reasonLabel("something_new"), "something new");
});

test("samples are told apart from findings", () => {
  assert.ok(isSample(item("sampled_low")));
  assert.ok(!isSample(item("rules_changed")));
});
