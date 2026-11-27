"use client";
import { useActionState, useState } from "react";
import { useRouter } from "next/navigation";
import { requestUndo, type ActionState } from "@/app/actions";
import { Notice } from "@/components/Notice";
import { SubmitButton } from "@/components/SubmitButton";
import type { UndoCardModel } from "@/lib/undo-view";

export function UndoCard({ runId, stepId, card }: { runId: string; stepId: string; card: UndoCardModel }) {
  const [open, setOpen] = useState(false);
  const [state, action, pending] = useActionState<ActionState, FormData>(requestUndo, null);
  const router = useRouter();
  const panelId = `undo-${encodeURIComponent(stepId)}`;
  return <section className="stack" aria-label={`Undo step ${stepId}`}>
    <div className="row">
      <span>{card.status}</span>
      {card.canUndo && <button type="button" aria-expanded={open} aria-controls={panelId}
        disabled={pending} onClick={() => setOpen(!open)}>{open ? "Close undo details" : "Undo"}</button>}
      <button type="button" className="link" disabled={pending} onClick={() => router.refresh()}>Refresh undo status</button>
    </div>
    <Notice state={state} />
    {open && card.canUndo && <form id={panelId} action={action} className="card stack">
      <h2>Undo rating · step {stepId}</h2>
      <dl>
        <dt>Rating written by this step</dt><dd>{card.writtenRating}</dd>
        <dt>Previous rating to restore</dt><dd>{card.priorRating}</dd>
      </dl>
      <p className="muted">These are captured values, not a live read of the contract.</p>
      <p>{card.revisionNote}</p>
      <p>{card.approvalNote}</p>
      <div>
        <strong>Later steps that used this output</strong>
        {card.laterSteps.length ? <ul>{card.laterSteps.map(step =>
          <li key={step.step_id}>{step.step_id} · {step.tool} · {step.outcome}</li>
        )}</ul> : <p>No later steps in this run referenced this output.</p>}
        <p className="notice wait">{card.effectsNote}</p>
      </div>
      {card.priorSummary !== null && <details>
        <summary>Previous summary · model-written text</summary><pre>{card.priorSummary}</pre>
      </details>}
      <input type="hidden" name="run_id" value={runId} />
      <input type="hidden" name="step_id" value={stepId} />
      <SubmitButton>Request undo</SubmitButton>
      {state?.error && <p>Refresh this status before making another request.</p>}
    </form>}
  </section>;
}
