// Pure presentation helpers (no framework imports, so they run under the plain Node test runner).

export type Tone = "good" | "bad" | "wait" | "run" | "neutral";

const TONES: Record<string, Tone> = {
  completed: "good",
  succeeded: "good",
  verified: "good",
  allow: "good",
  approved: "good",
  failed: "bad",
  denied: "bad",
  rejected: "bad",
  cancelled: "bad",
  expired: "bad",
  paused_for_approval: "wait",
  awaiting_approval: "wait",
  needs_approval: "wait",
  pending: "wait",
  running: "run",
  executing: "run",
  verifying: "run",
  ready: "run",
  policy_checked: "run",
  planning: "run",
  created: "run",
};

export function toneFor(status: string): Tone {
  return TONES[status] ?? "neutral";
}

export function label(status: string): string {
  return status.replaceAll("_", " ");
}

export function shortHash(hash: string | null | undefined, length = 12): string {
  if (!hash) return "-";
  return hash.length <= length ? hash : `${hash.slice(0, length)}…`;
}

export function relativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "-";
  const seconds = Math.round((now.getTime() - then) / 1000);
  const abs = Math.abs(seconds);
  const unit =
    abs < 60 ? `${abs}s` : abs < 3600 ? `${Math.floor(abs / 60)}m` : abs < 86400
      ? `${Math.floor(abs / 3600)}h`
      : `${Math.floor(abs / 86400)}d`;
  return seconds >= 0 ? `${unit} ago` : `in ${unit}`;
}

export function isFinished(status: string): boolean {
  return ["completed", "failed", "denied", "cancelled"].includes(status);
}

/** The verify route's anchor report (see docs/api.md). */
export type AnchorReport = {
  status: "none" | "matched" | "mismatch" | "evidence_invalid" | "unavailable";
  source: "stored" | "caller" | null;
  sink: string | null;
  external: boolean;
  seq: number | null;
  timestamped_at: string | null;
  events_after_anchor: number;
};

export type AnchorNotice = { tone: "ok" | "error" | "wait"; text: string };

/** One line saying how the chain compares with the copy of its head kept outside the database. */
export function anchorNotice(anchor: AnchorReport | null | undefined): AnchorNotice {
  if (!anchor || anchor.status === "none") {
    return { tone: "wait", text: "No anchor outside the database for this run yet" };
  }
  if (anchor.status === "mismatch") {
    return {
      tone: "error",
      text: `Anchor MISMATCH: history changed or was cut short at or before event ${anchor.seq ?? "?"}`,
    };
  }
  if (anchor.status === "evidence_invalid") {
    return { tone: "error", text: "Anchor INVALID: the stored anchor fails its own check" };
  }
  if (anchor.status === "unavailable") {
    return { tone: "wait", text: "Anchor could not be checked right now" };
  }
  const what =
    anchor.source === "caller"
      ? "your copy"
      : anchor.external
        ? `a signed timestamp${anchor.timestamped_at ? ` from ${utc(anchor.timestamped_at)}` : ""}`
        : "a local copy (not independent)";
  const later =
    anchor.events_after_anchor > 0
      ? `; ${anchor.events_after_anchor} event${anchor.events_after_anchor === 1 ? "" : "s"} added after it`
      : "";
  return { tone: "ok", text: `Anchor matched at event ${anchor.seq ?? "?"}: ${what}${later}` };
}

function utc(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "-" : `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}
