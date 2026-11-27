import assert from "node:assert/strict";
import { test } from "node:test";
import type { DemoInfo } from "../lib/api-types.ts";
import { demoEnabled, isLocalHost, modelLabel, personByKey, personFor, readSignIn } from "../lib/demo-view.ts";

export const INFO: DemoInfo = {
  model: "gawe-demo-scripted",
  companies: [
    {
      key: "careful",
      name: "Careful Co.",
      tenant_id: "t-careful",
      summary: "Every low rating needs legal.",
      people: [
        { key: "careful-alice", name: "Alice", title: "case manager", roles: ["case_manager"] },
        { key: "careful-bob", name: "Bob", title: "legal reviewer", roles: ["legal_reviewer"] },
      ],
    },
  ],
  scenes: [],
  fixture: { company: "careful", run_id: "run-1" },
};

test("the label names a scripted run's mode, and nothing for a real model's run", () => {
  assert.equal(modelLabel("honest"), "Scripted demo model: honest");
  assert.equal(modelLabel("fooled"), "Scripted demo model: fooled");
  assert.equal(modelLabel(null), null);
  assert.equal(modelLabel(undefined), null);
  assert.equal(modelLabel("anything"), null);
});

test("only this machine's names count as localhost", () => {
  for (const host of ["localhost", "localhost:3000", "127.0.0.1", "127.0.0.1:3000", "[::1]:3000", "[::1]"]) {
    assert.equal(isLocalHost(host), true, host);
  }
  for (const host of ["", null, undefined, "example.com", "localhost.evil.example", "127.0.0.1.evil.example", "10.0.0.5:3000", "evil.example:localhost"]) {
    assert.equal(isLocalHost(host), false, String(host));
  }
});

test("the demo pages need the flag, the API's demo info and a local request", () => {
  assert.equal(demoEnabled("1", "localhost:3000", INFO), true);
  assert.equal(demoEnabled(undefined, "localhost:3000", INFO), false);
  assert.equal(demoEnabled("0", "localhost:3000", INFO), false);
  assert.equal(demoEnabled("1", "localhost:3000", null), false);
  assert.equal(demoEnabled("1", "demo.example.com", INFO), false);
});

test("people are found by key and by role", () => {
  assert.equal(personByKey(INFO, "careful-bob")?.person.name, "Bob");
  assert.equal(personByKey(INFO, "careful-bob")?.company.key, "careful");
  assert.equal(personByKey(INFO, "nobody"), null);
  assert.equal(personFor(INFO.companies[0]!, "legal_reviewer")?.key, "careful-bob");
  assert.equal(personFor(INFO.companies[0]!, "finance_approver"), null);
});

test("a sign-in file must map people to tokens", () => {
  const token = "t".repeat(40);
  assert.deepEqual(readSignIn(JSON.stringify({ people: { "careful-alice": token } })), { "careful-alice": token });
  for (const raw of ["{}", '{"people": null}', '{"people": {"a": 1}}', '{"people": {"a": "short"}}']) {
    assert.throws(() => readSignIn(raw), raw);
  }
});
