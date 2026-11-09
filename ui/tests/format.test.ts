import assert from "node:assert/strict";
import { test } from "node:test";
import {
  type AnchorReport,
  anchorNotice,
  isFinished,
  label,
  relativeTime,
  shortHash,
  toneFor,
} from "../lib/format.ts";

test("known statuses get a tone and unknown ones are neutral", () => {
  assert.equal(toneFor("completed"), "good");
  assert.equal(toneFor("paused_for_approval"), "wait");
  assert.equal(toneFor("denied"), "bad");
  assert.equal(toneFor("something_new"), "neutral");
});

test("label replaces underscores", () => {
  assert.equal(label("paused_for_approval"), "paused for approval");
});

test("shortHash handles null, short and long values", () => {
  assert.equal(shortHash(null), "-");
  assert.equal(shortHash(undefined), "-");
  assert.equal(shortHash("abc"), "abc");
  assert.equal(shortHash("a".repeat(64)), "a".repeat(12) + "…");
});

test("relativeTime covers past, future and bad input", () => {
  const now = new Date("2026-01-01T00:10:00Z");
  assert.equal(relativeTime("2026-01-01T00:09:30Z", now), "30s ago");
  assert.equal(relativeTime("2026-01-01T00:05:00Z", now), "5m ago");
  assert.equal(relativeTime("2025-12-31T20:10:00Z", now), "4h ago");
  assert.equal(relativeTime("2025-12-29T00:10:00Z", now), "3d ago");
  assert.equal(relativeTime("2026-01-01T00:40:00Z", now), "in 30m");
  assert.equal(relativeTime("not a date", now), "-");
});

test("isFinished", () => {
  assert.equal(isFinished("completed"), true);
  assert.equal(isFinished("paused_for_approval"), false);
});

const ANCHOR: AnchorReport = {
  status: "matched",
  source: "stored",
  sink: "file",
  external: false,
  seq: 14,
  timestamped_at: null,
  events_after_anchor: 0,
};

test("anchorNotice says what was matched and how independent it is", () => {
  assert.deepEqual(anchorNotice(ANCHOR), {
    tone: "ok",
    text: "Anchor matched at event 14: a local copy (not independent)",
  });
  const signed = { ...ANCHOR, sink: "rfc3161", external: true, timestamped_at: "2026-10-30T00:11:25Z" };
  assert.equal(
    anchorNotice(signed).text,
    "Anchor matched at event 14: a signed timestamp from 2026-10-30 00:11 UTC",
  );
  assert.equal(anchorNotice({ ...ANCHOR, source: "caller" }).text, "Anchor matched at event 14: your copy");
  assert.equal(
    anchorNotice({ ...ANCHOR, events_after_anchor: 2 }).text,
    "Anchor matched at event 14: a local copy (not independent); 2 events added after it",
  );
});

test("anchorNotice flags mismatches and invalid evidence as errors", () => {
  assert.equal(anchorNotice({ ...ANCHOR, status: "mismatch" }).tone, "error");
  assert.match(anchorNotice({ ...ANCHOR, status: "mismatch" }).text, /MISMATCH.*event 14/);
  assert.equal(anchorNotice({ ...ANCHOR, status: "evidence_invalid" }).tone, "error");
});

test("anchorNotice handles no anchor and an unreachable anchor", () => {
  assert.equal(anchorNotice(undefined).tone, "wait");
  assert.equal(anchorNotice(null).text, "No anchor outside the database for this run yet");
  assert.equal(anchorNotice({ ...ANCHOR, status: "none" }).tone, "wait");
  assert.equal(anchorNotice({ ...ANCHOR, status: "unavailable" }).text, "Anchor could not be checked right now");
});
