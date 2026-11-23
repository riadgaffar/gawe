"use client";
import Link from "next/link";
import { useActionState } from "react";
import { markHandoff, type ActionState } from "@/app/actions";
import { Notice } from "@/components/Notice";
import { SubmitButton } from "@/components/SubmitButton";
import type { HandoffView } from "@/lib/api-types";
import { handoffCardModel } from "@/lib/handoff-view";

// A prepare-only action (M20): the engine prepared it and will never send it. The person does it
// by hand in the target system, then says whether they did.
export function HandoffCard({ handoff }: { handoff: HandoffView }) {
  const [state, action] = useActionState<ActionState, FormData>(markHandoff, null);
  const done = Boolean(state?.ok);
  const card = handoffCardModel(handoff);
  return (
    <form action={action} className="card stack">
      <div className="row">
        <h2><code>{handoff.tool}</code></h2>
        <span className="badge tone-wait">{handoff.risk_tier} risk</span>
        <span className="muted">for {handoff.role}</span>
      </div>
      <p>GAWE prepared this and will not send it. Do it yourself in the target system, then mark it.</p>
      {card.policy && <p className="muted">{card.policy}</p>}
      <p className="muted">
        Requested by {handoff.requested_by} ·{" "}
        <Link href={`/runs/${encodeURIComponent(handoff.run_id)}`}>run</Link> · step {handoff.step_id}
      </p>
      <div>
        <strong>Exactly what to do</strong>
        <dl>
          {card.typed.map((row) => (
            <div key={row.path}>
              <dt><code>{row.path}</code></dt>
              <dd>{row.display}</dd>
            </div>
          ))}
        </dl>
      </div>
      <div>
        <strong>Action hash</strong> <span className="muted">(your mark is bound to this)</span>
        <pre>{card.actionHash}</pre>
      </div>
      {card.freeText.map((section) => (
        <details key={section.path}>
          <summary><code>{section.path}</code> {section.header}</summary>
          <pre>{section.content}</pre>
        </details>
      ))}
      <input type="hidden" name="run_id" value={handoff.run_id} />
      <input type="hidden" name="step_id" value={handoff.step_id} />
      <input type="hidden" name="action_hash" value={handoff.action_hash} />
      <label>
        Reference (optional, for done: a ticket or envelope id)
        <input name="reference" maxLength={120} pattern="[A-Za-z0-9._:\-]{1,120}" disabled={done} />
      </label>
      <Notice state={state} />
      {!done && (
        <div className="row">
          <SubmitButton name="outcome" value="done">Done</SubmitButton>
          {/* The reference only goes with done, so a half-typed one never blocks "not done". */}
          <SubmitButton name="outcome" value="not_done" className="danger" formNoValidate>
            Not done
          </SubmitButton>
        </div>
      )}
    </form>
  );
}
