// Server-only (M29): the demo's info from the API, the gate, and the people's tokens.
import { readFile } from "node:fs/promises";
import path from "node:path";
import { headers } from "next/headers";
import type { DemoInfo } from "./api-types";
import { demoEnabled, readSignIn } from "./demo-view";
import { apiBase } from "./session";

/** /v1/demo, which needs no sign-in; null when the API isn't running the demo model. */
export async function fetchDemoInfo(): Promise<DemoInfo | null> {
  try {
    const response = await fetch(`${apiBase().replace(/\/+$/, "")}/v1/demo`, { cache: "no-store" });
    return response.ok ? ((await response.json()) as DemoInfo) : null;
  } catch {
    return null;
  }
}

/** The demo's info when the demo pages may be shown for this request; null otherwise. */
export async function demoGate(): Promise<DemoInfo | null> {
  const host = (await headers()).get("host");
  if (process.env.GAWE_UI_DEMO !== "1") return null;
  const info = await fetchDemoInfo();
  return demoEnabled(process.env.GAWE_UI_DEMO, host, info) ? info : null;
}

/** The demo people's tokens, from the file `make demo` writes (GAWE_UI_DEMO_SIGNIN). */
export async function demoTokens(): Promise<Record<string, string>> {
  const file = process.env.GAWE_UI_DEMO_SIGNIN;
  if (!file) throw new Error("GAWE_UI_DEMO_SIGNIN is not set");
  return readSignIn(await readFile(file, "utf8"));
}

/** A scene's sample document, from the UI's own copy of the samples. */
export async function sampleText(file: string): Promise<string> {
  if (!/^[A-Za-z0-9._-]+\.md$/.test(file)) throw new Error("not a sample file");
  return readFile(path.join(process.cwd(), "public", "samples", file), "utf8");
}
