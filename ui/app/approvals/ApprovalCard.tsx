"use client";
import Link from "next/link";
import { useActionState } from "react";
import { decide, type ActionState } from "@/app/actions";
import { Notice } from "@/components/Notice";
import { SubmitButton } from "@/components/SubmitButton";
import type { ApprovalView } from "@/lib/api-types";
import { relativeTime } from "@/lib/format";

export function ApprovalCard({ approval }: { approval: ApprovalView }) {
  const [state, action] = useActionState<ActionState, FormData>(decide, null);
  const done = Boolean(state?.ok);
  return (
    <form action={action} className="card stack">
      <div className="row">
        <h2><code>{approval.tool}</code></h2>
        <span className="badge tone-wait">{approval.risk_tier} risk</span>
        <span className="muted">needs {approval.approver_role}</span>
      </div>
      <p>{approval.reason}</p>
      <p className="muted">
        Requested by {approval.requested_by} {relativeTime(approval.requested_at)} · expires{" "}
        {relativeTime(approval.expires_at)} ·{" "}
        <Link href={`/runs/${encodeURIComponent(approval.run_id)}`}>run</Link> · step {approval.step_id}
      </p>
      <div>
        <strong>Exactly what will run</strong>
        <pre>{JSON.stringify(approval.resolved_args, null, 2)}</pre>
      </div>
      <div>
        <strong>Action hash</strong> <span className="muted">(your decision is bound to this)</span>
        <pre>{approval.action_hash}</pre>
      </div>
      <input type="hidden" name="approval_id" value={approval.approval_id} />
      <input type="hidden" name="action_hash" value={approval.action_hash} />
      <label>
        Comment (optional)
        <input name="comment" maxLength={2000} disabled={done} />
      </label>
      <Notice state={state} />
      {!done && (
        <div className="row">
          <SubmitButton name="decision" value="approved">Approve</SubmitButton>
          <SubmitButton name="decision" value="rejected" className="danger">Reject</SubmitButton>
        </div>
      )}
    </form>
  );
}
