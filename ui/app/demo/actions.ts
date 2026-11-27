"use server";
// The demo's actions (M29). Each checks the demo gate again: a form posted from elsewhere, or
// after the API stopped running the demo model, gets nothing.
import { notFound, redirect } from "next/navigation";
import type { ActionState } from "@/app/actions";
import { ApiError } from "@/lib/api-client";
import { demoGate, demoTokens, sampleText } from "@/lib/demo";
import { personByKey, personFor } from "@/lib/demo-view";
import { clientFor, setToken } from "@/lib/session";

function text(form: FormData, key: string): string {
  const value = form.get(key);
  return typeof value === "string" ? value : "";
}

function internal(next: string): string {
  return next.startsWith("/") && !next.startsWith("//") ? next : "/runs";
}

export async function demoSignIn(form: FormData): Promise<void> {
  const info = await demoGate();
  if (!info) notFound();
  const found = personByKey(info, text(form, "person"));
  if (!found) notFound();
  const token = (await demoTokens())[found.person.key];
  if (!token) notFound();
  await setToken(token);
  redirect(internal(text(form, "next")));
}

export async function demoStartScene(_prev: ActionState, form: FormData): Promise<ActionState> {
  const info = await demoGate();
  if (!info) notFound();
  const company = info.companies.find((c) => c.key === text(form, "company"));
  const scene = info.scenes.find((s) => s.scene_id === text(form, "scene"));
  const mode = text(form, "mode");
  if (!company || !scene || (mode !== "honest" && mode !== "fooled") || !scene.modes.includes(mode)) {
    return { error: "That scene isn't part of the demo." };
  }
  const starter = personFor(company, "case_manager");
  const token = starter ? (await demoTokens())[starter.key] : undefined;
  if (!starter || !token) return { error: "The demo has no case manager for that company." };
  let runId: string;
  try {
    const run = await clientFor(token).startRun({
      objective: scene.objective,
      profile: "contract_review",
      document: await sampleText(scene.document),
      model_mode: mode,
    });
    runId = run.run_id;
  } catch (error) {
    return { error: error instanceof ApiError ? error.message : "The run could not start." };
  }
  await setToken(token);
  redirect(`/runs/${encodeURIComponent(runId)}`);
}
