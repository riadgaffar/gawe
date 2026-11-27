"use server";
// Every mutation goes through here. The UI relays what the user chose; the API decides.
import { revalidatePath } from "next/cache";
import { undoReason } from "@/lib/undo-view";
import { redirect } from "next/navigation";
import { ApiError } from "@/lib/api-client";
import { clearToken, clientFor, requireSession, setToken } from "@/lib/session";

export type ActionState = { error?: string; ok?: string } | null;

const MAX_UPLOAD_BYTES = 1_000_000;

function fail(error: unknown): ActionState {
  if (error instanceof ApiError) {
    if (error.status === 0) return { error: error.message };
    if (error.status === 403) return { error: "You are not allowed to do that." };
    if (error.status === 404) return { error: "Not found, or it belongs to another tenant." };
    if (error.status === 409) {
      return { error: `${error.message} Reload the page: it may have changed since you opened it.` };
    }
    return { error: error.message };
  }
  return { error: "Something went wrong." };
}

function text(form: FormData, key: string): string {
  const value = form.get(key);
  return typeof value === "string" ? value : "";
}

export async function login(_prev: ActionState, form: FormData): Promise<ActionState> {
  const token = text(form, "token").trim();
  if (!token) return { error: "Paste a token." };
  try {
    await clientFor(token).me();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return { error: "That token was not accepted." };
    }
    return fail(error);
  }
  await setToken(token);
  redirect("/runs");
}

export async function logout(): Promise<void> {
  await clearToken();
  redirect("/login");
}

async function documentFrom(form: FormData): Promise<string | undefined | { error: string }> {
  const file = form.get("file");
  if (file instanceof File && file.size > 0) {
    if (file.size > MAX_UPLOAD_BYTES) return { error: "The file is larger than 1 MB." };
    if (!/\.(txt|md)$/i.test(file.name)) return { error: "Only .txt and .md files are supported." };
    return await file.text();
  }
  const pasted = text(form, "document");
  return pasted.trim() ? pasted : undefined;
}

export async function startRun(_prev: ActionState, form: FormData): Promise<ActionState> {
  const { client } = await requireSession();
  const objective = text(form, "objective").trim();
  const profile = text(form, "profile").trim();
  if (!objective || !profile) return { error: "Objective and profile are required." };
  const sourceId = text(form, "source_id").trim();
  const doc = await documentFrom(form);
  if (typeof doc === "object") return doc;
  if (sourceId && doc) return { error: "Give a ticket id or a document, not both." };
  let runId: string;
  try {
    runId = (
      await client.startRun({ objective, profile, document: doc, source_id: sourceId || undefined })
    ).run_id;
  } catch (error) {
    return fail(error);
  }
  redirect(`/runs/${encodeURIComponent(runId)}`);
}

export async function cancelRun(_prev: ActionState, form: FormData): Promise<ActionState> {
  const { client } = await requireSession();
  try {
    await client.cancelRun(text(form, "run_id"));
    return { ok: "Cancelled." };
  } catch (error) {
    return fail(error);
  }
}

export async function resumeRun(_prev: ActionState, form: FormData): Promise<ActionState> {
  const { client } = await requireSession();
  try {
    await client.resumeRun(text(form, "run_id"));
    return { ok: "Resumed." };
  } catch (error) {
    return fail(error);
  }
}

export async function decide(_prev: ActionState, form: FormData): Promise<ActionState> {
  const { client } = await requireSession();
  const decision = text(form, "decision");
  if (decision !== "approved" && decision !== "rejected") return { error: "Choose approve or reject." };
  const comment = text(form, "comment").trim();
  try {
    // The hash is the one the page displayed: the API refuses it if the action changed.
    await client.decide(text(form, "approval_id"), {
      decision,
      action_hash: text(form, "action_hash"),
      comment: comment || undefined,
    });
    return { ok: decision === "approved" ? "Approved." : "Rejected." };
  } catch (error) {
    return fail(error);
  }
}

// Mirrors the server's rule for a hand-off reference (a ticket or envelope id).
const REFERENCE = /^[A-Za-z0-9._:-]{1,120}$/;

export async function markHandoff(_prev: ActionState, form: FormData): Promise<ActionState> {
  const { client } = await requireSession();
  const outcome = text(form, "outcome");
  if (outcome !== "done" && outcome !== "not_done") return { error: "Choose done or not done." };
  // A reference only goes with done; one left half-typed never blocks "not done".
  const reference = outcome === "done" ? text(form, "reference").trim() : "";
  if (reference && !REFERENCE.test(reference)) {
    return { error: "A reference is up to 120 letters, digits, dots, underscores, colons or dashes." };
  }
  try {
    // The hash is the one the page displayed: the API refuses it if the action changed.
    await client.markHandoff(text(form, "run_id"), text(form, "step_id"), {
      outcome,
      action_hash: text(form, "action_hash"),
      reference: reference || undefined,
    });
    return { ok: outcome === "done" ? "Marked done." : "Marked not done." };
  } catch (error) {
    return fail(error);
  }
}

export async function reviewVerdict(_prev: ActionState, form: FormData): Promise<ActionState> {
  const { client } = await requireSession();
  const verdict = text(form, "verdict");
  if (verdict !== "confirmed" && verdict !== "missed_risk") return { error: "Choose a verdict." };
  try {
    await client.reviewVerdict(text(form, "item_id"), verdict);
    return { ok: verdict === "confirmed" ? "Confirmed." : "Recorded as a missed risk." };
  } catch (error) {
    return fail(error);
  }
}

export type VerifyState = {
  error?: string;
  result?: { ok: boolean; checked: number; head: string | null; failed_seq: number | null; reason: string | null };
} | null;

export async function verifyAudit(_prev: VerifyState, form: FormData): Promise<VerifyState> {
  const { client } = await requireSession();
  try {
    return { result: await client.verifyAudit(text(form, "run_id")) };
  } catch (error) {
    return { error: fail(error)?.error };
  }
}


export async function requestUndo(_prev: ActionState, form: FormData): Promise<ActionState> {
  const { client, me } = await requireSession();
  if (!me.roles.includes("case_manager")) return { error: undoReason("forbidden") };
  const runId = text(form, "run_id");
  const stepId = text(form, "step_id");
  if (!runId || !stepId) return { error: "Choose a run and step." };
  let undoRunId: string;
  try {
    // No rating, revision or prior text comes from the form; trusted API code supplies them.
    undoRunId = (await client.requestUndo(runId, stepId)).run_id;
  } catch (error) {
    revalidatePath(`/runs/${encodeURIComponent(runId)}`);
    return { error: error instanceof ApiError ? undoReason(error.code) : "The request outcome is unknown. Refresh the status before trying again." };
  }
  revalidatePath(`/runs/${encodeURIComponent(runId)}`);
  redirect(`/runs/${encodeURIComponent(undoRunId)}`);
}
