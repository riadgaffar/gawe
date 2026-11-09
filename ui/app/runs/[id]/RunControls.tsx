"use client";
import { useActionState } from "react";
import { cancelRun, resumeRun, type ActionState } from "@/app/actions";
import { Notice } from "@/components/Notice";
import { SubmitButton } from "@/components/SubmitButton";

export function RunControls({ runId }: { runId: string }) {
  const [cancelState, cancel] = useActionState<ActionState, FormData>(cancelRun, null);
  const [resumeState, resume] = useActionState<ActionState, FormData>(resumeRun, null);
  return (
    <div className="row">
      <form action={resume}>
        <input type="hidden" name="run_id" value={runId} />
        <SubmitButton>Resume</SubmitButton>
      </form>
      <form action={cancel}>
        <input type="hidden" name="run_id" value={runId} />
        <SubmitButton className="danger">Cancel run</SubmitButton>
      </form>
      <Notice state={cancelState} />
      <Notice state={resumeState} />
    </div>
  );
}
