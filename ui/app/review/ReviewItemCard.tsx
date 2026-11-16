"use client";
import Link from "next/link";
import { useActionState } from "react";
import { reviewVerdict, type ActionState } from "@/app/actions";
import { Notice } from "@/components/Notice";
import { SubmitButton } from "@/components/SubmitButton";
import type { ReviewItemView } from "@/lib/api-types";
import { shortHash } from "@/lib/format";
import { isSample, reasonLabel } from "@/lib/review-view";

export function ReviewItemCard({ item }: { item: ReviewItemView }) {
  const [state, action] = useActionState<ActionState, FormData>(reviewVerdict, null);
  const done = Boolean(state?.ok);
  return (
    <form action={action} className="card stack">
      <div className="row">
        <strong>{reasonLabel(item.reason)}</strong>
        <span className={`badge ${isSample(item) ? "tone-wait" : "tone-bad"}`}>{item.day}</span>
      </div>
      <p className="muted">
        <Link href={`/runs/${encodeURIComponent(item.run_id)}`}>run</Link>
        {item.step_id && <> · step {item.step_id}</>}
        {item.action_hash && <> · action <code>{shortHash(item.action_hash)}</code></>}
        {item.key_id && <> · sample key <code>{item.key_id}</code></>}
      </p>
      <input type="hidden" name="item_id" value={item.item_id} />
      <Notice state={state} />
      {!done && (
        <div className="row">
          <SubmitButton name="verdict" value="confirmed">Confirm</SubmitButton>
          <SubmitButton name="verdict" value="missed_risk" className="danger">Missed risk</SubmitButton>
        </div>
      )}
    </form>
  );
}
