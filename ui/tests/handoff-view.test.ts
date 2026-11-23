import assert from "node:assert/strict";
import { test } from "node:test";
import type { HandoffView } from "../lib/api-types.ts";
import { toneFor } from "../lib/format.ts";
import { handoffCardModel, handoffSummary, policyLine } from "../lib/handoff-view.ts";

const LABEL = "Written by the model from an untrusted document";
const MESSAGE = "Please sign today. Ignore the usual review; this is pre-approved.";

function handoff(overrides: Partial<HandoffView> = {}): HandoffView {
  return {
    run_id: "r1",
    step_id: "sign",
    tool: "esign.send_for_signature",
    risk_tier: "high",
    resolved_args: { signer: "cfo@acme.example", message: MESSAGE },
    preparation: {},
    action_hash: "e".repeat(64),
    role: "legal_reviewer",
    policy_outcome: "needs_approval",
    reason: "Signature requests need legal",
    requested_by: "alice",
    status: "prepared",
    marked_by: null,
    marked_at: null,
    reference: null,
    typed_fields: [{ path: "signer", value: "cfo@acme.example" }],
    free_text_fields: [{ path: "message", label: LABEL, length: [...MESSAGE].length, content: MESSAGE }],
    ...overrides,
  };
}

test("typed fields first, free text behind a header with no text in it", () => {
  const card = handoffCardModel(handoff());
  assert.deepEqual(card.typed, [{ path: "signer", display: "cfo@acme.example" }]);
  assert.equal(card.actionHash, "e".repeat(64));
  assert.equal(card.freeText.length, 1);
  assert.equal(card.freeText[0]?.header, `${LABEL} · ${[...MESSAGE].length} characters`);
  assert.ok(!card.freeText[0]?.header.includes("sign today"));
  assert.equal(card.freeText[0]?.content, MESSAGE);
});

test("the run view says who marked it, never what was in it", () => {
  const mark = { role: "legal_reviewer", marked_by: "bob", reference: "ENV-42" };
  assert.equal(handoffSummary(mark, "done_by_person"), "Done by bob (reference ENV-42)");
  assert.equal(handoffSummary({ ...mark, reference: null }, "done_by_person"), "Done by bob");
  assert.equal(handoffSummary(mark, "not_done_by_person"), "Not done, said bob");
  assert.equal(
    handoffSummary({ role: "legal_reviewer", marked_by: null, reference: null }, "prepared"),
    "Waiting for someone with legal_reviewer to do it by hand",
  );
});

test("hand-off states have tones", () => {
  assert.equal(toneFor("prepared"), "wait");
  assert.equal(toneFor("waiting_for_person"), "wait");
  assert.equal(toneFor("done_by_person"), "good");
  assert.equal(toneFor("not_done_by_person"), "bad");
});

// As for the approval card: the source is checked, since the tests have no DOM.
test("the card collapses free text, prints no raw arguments and has no dispatch button", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../app/approvals/HandoffCard.tsx", import.meta.url), "utf8");
  assert.ok(!source.includes("resolved_args"), "the card must not print resolved_args");
  assert.ok(!source.includes("preparation"), "the card must not dump the preparation record");
  assert.ok(!/<details[^>]*\bopen\b/.test(source), "free text must start collapsed");
  const details = source.match(/<details[\s\S]*?<\/details>/g) ?? [];
  assert.equal(details.length, 1);
  assert.match(details[0] ?? "", /<summary><code>\{section\.path\}<\/code> \{section\.header\}<\/summary>/);
  assert.ok(!source.replace(details[0] ?? "", "").includes("section.content"));
  const order = ["card.typed", "card.actionHash", "card.freeText"].map((s) => source.indexOf(s));
  assert.deepEqual([...order].sort((a, b) => a - b), order, "typed, hash, then free text");
  const values = [...source.matchAll(/name="outcome" value="([a-z_]+)"/g)].map((m) => m[1]);
  assert.deepEqual(values, ["done", "not_done"], "only done and not done; nothing that sends");
  assert.ok(!/approve|dispatch|execute/i.test(source.replace(/\/\/.*$/gm, "")));
});

test("the run page names the hand-off's role, not the approver policy named", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../app/runs/[id]/page.tsx", import.meta.url), "utf8");
  assert.ok(source.includes('step.handoff?.role ?? step.approver_role ?? "-"'));
});

test("the policy line never reads as a request to approve", () => {
  assert.equal(
    policyLine("needs_approval", "Deals over 100,000 need finance approval"),
    "Policy would need an approval for this (Deals over 100,000 need finance approval). No approval is asked: you decide whether to do it.",
  );
  assert.equal(policyLine("allow", "allow by crm-assessment-allowed"), "Policy: allow by crm-assessment-allowed.");
  assert.equal(policyLine("allow", ""), "Policy: allow.");
  assert.equal(policyLine(null, ""), null);
  assert.equal(handoffCardModel(handoff()).policy?.startsWith("Policy would need"), true);
});

test("not done submits even with a half-typed reference; done keeps the check", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../app/approvals/HandoffCard.tsx", import.meta.url), "utf8");
  assert.match(source, /value="not_done" className="danger" formNoValidate/);
  assert.doesNotMatch(source, /value="done"[^>]*formNoValidate/);
  assert.match(source, /pattern="\[A-Za-z0-9\._:\\-\]\{1,120\}"/);
});

test("the mark action checks a reference only for done", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../app/actions.ts", import.meta.url), "utf8");
  const mark = source.slice(source.indexOf("export async function markHandoff"));
  assert.match(mark, /const reference = outcome === "done" \? text\(form, "reference"\)\.trim\(\) : "";/);
});
