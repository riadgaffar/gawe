import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError, createClient, type FetchLike } from "../lib/api-client.ts";

function fake(status: number, body: unknown, seen: { url?: string; init?: RequestInit } = {}): FetchLike {
  return async (url, init) => {
    seen.url = url;
    seen.init = init;
    return new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
  };
}

test("sends the bearer token and parses the body", async () => {
  const seen: { url?: string; init?: RequestInit } = {};
  const client = createClient({
    baseUrl: "http://api.test/",
    token: "tok",
    fetchImpl: fake(200, { user_id: "bob", tenant_id: "t1", roles: ["legal_reviewer"] }, seen),
  });
  const me = await client.me();
  assert.equal(me.user_id, "bob");
  assert.equal(seen.url, "http://api.test/v1/me");
  assert.equal((seen.init?.headers as Record<string, string>).Authorization, "Bearer tok");
});

test("posts JSON bodies with a content type", async () => {
  const seen: { url?: string; init?: RequestInit } = {};
  const client = createClient({ baseUrl: "http://a", token: "t", fetchImpl: fake(201, {}, seen) });
  await client.startRun({ objective: "o", profile: "p" });
  assert.equal(seen.init?.method, "POST");
  assert.equal(seen.init?.body, JSON.stringify({ objective: "o", profile: "p" }));
  assert.equal((seen.init?.headers as Record<string, string>)["Content-Type"], "application/json");
});

test("ids are url-encoded", async () => {
  const seen: { url?: string } = {};
  const client = createClient({ baseUrl: "http://a", token: "t", fetchImpl: fake(200, {}, seen) });
  await client.getRun("a/b?c");
  assert.equal(seen.url, "http://a/v1/runs/a%2Fb%3Fc");
});

for (const [status, code] of [
  [401, "unauthorized"],
  [403, "forbidden"],
  [404, "not_found"],
  [409, "conflict"],
] as const) {
  test(`maps ${status} to ApiError(${code})`, async () => {
    const client = createClient({
      baseUrl: "http://a",
      token: "t",
      fetchImpl: fake(status, { error: { code, message: "nope" } }),
    });
    await assert.rejects(client.listRuns(), (e: unknown) => {
      assert.ok(e instanceof ApiError);
      assert.equal(e.status, status);
      assert.equal(e.code, code);
      assert.equal(e.message, "nope");
      return true;
    });
  });
}

test("a non-JSON error body still gives a clean error", async () => {
  const client = createClient({ baseUrl: "http://a", token: "t", fetchImpl: fake(502, "<html>") });
  await assert.rejects(client.me(), (e: unknown) => e instanceof ApiError && e.status === 502);
});

test("network failure becomes an unreachable error without leaking details", async () => {
  const client = createClient({
    baseUrl: "http://a",
    token: "secret-token",
    fetchImpl: async () => {
      throw new Error("ECONNREFUSED secret-token");
    },
  });
  await assert.rejects(client.me(), (e: unknown) => {
    assert.ok(e instanceof ApiError);
    assert.equal(e.code, "unreachable");
    assert.ok(!e.message.includes("secret-token"));
    return true;
  });
});
