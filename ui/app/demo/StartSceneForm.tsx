"use client";
import { useActionState } from "react";
import type { ActionState } from "@/app/actions";
import { Notice } from "@/components/Notice";
import { SubmitButton } from "@/components/SubmitButton";
import { demoStartScene } from "./actions";

export function StartSceneForm({
  company,
  scene,
  mode,
  label,
}: {
  company: string;
  scene: string;
  mode: "honest" | "fooled";
  label: string;
}) {
  const [state, action] = useActionState<ActionState, FormData>(demoStartScene, null);
  return (
    <form action={action} className="inline">
      <input type="hidden" name="company" value={company} />
      <input type="hidden" name="scene" value={scene} />
      <input type="hidden" name="mode" value={mode} />
      <SubmitButton>{label}</SubmitButton>
      <Notice state={state} />
    </form>
  );
}
