// The demo's view helpers (M29). Pure, so they run under the plain Node test runner.
import type { DemoCompany, DemoInfo, DemoPerson } from "./api-types.ts";

/** The label shown wherever a run's model is shown; null for a run under a real model. */
export function modelLabel(mode: string | null | undefined): string | null {
  if (mode === "honest") return "Scripted demo model: honest";
  if (mode === "fooled") return "Scripted demo model: fooled";
  return null;
}

/** Whether a Host header names this machine. The demo's sign-in is for localhost only. */
export function isLocalHost(host: string | null | undefined): boolean {
  if (!host) return false;
  const name = host.startsWith("[") ? host.slice(0, host.indexOf("]") + 1) : host.split(":")[0];
  return name === "localhost" || name === "127.0.0.1" || name === "[::1]";
}

/**
 * The demo pages exist only when all three hold: the UI was started for the demo
 * (GAWE_UI_DEMO=1), the API says it runs the scripted demo model (it answers /v1/demo), and
 * the request came to localhost. Anything else is "not found".
 */
export function demoEnabled(flag: string | undefined, host: string | null, info: DemoInfo | null): boolean {
  return flag === "1" && info !== null && isLocalHost(host);
}

export function personByKey(info: DemoInfo, key: string): { company: DemoCompany; person: DemoPerson } | null {
  for (const company of info.companies) {
    const person = company.people.find((p) => p.key === key);
    if (person) return { company, person };
  }
  return null;
}

export function personFor(company: DemoCompany, role: string): DemoPerson | null {
  return company.people.find((p) => p.roles.includes(role)) ?? null;
}

/** A sign-in file's contents: person key -> token. Anything else is rejected. */
export function readSignIn(raw: string): Record<string, string> {
  const data = JSON.parse(raw) as unknown;
  if (typeof data !== "object" || data === null || !("people" in data)) {
    throw new Error("the demo sign-in file has no people");
  }
  const people = (data as { people: unknown }).people;
  if (typeof people !== "object" || people === null) throw new Error("the demo sign-in file is malformed");
  const out: Record<string, string> = {};
  for (const [key, token] of Object.entries(people)) {
    if (typeof token !== "string" || token.length < 20) throw new Error("the demo sign-in file is malformed");
    out[key] = token;
  }
  return out;
}
