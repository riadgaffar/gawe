import Link from "next/link";
import { notFound } from "next/navigation";
import { Shell } from "@/components/Shell";
import { StatusBadge } from "@/components/StatusBadge";
import { ApiError } from "@/lib/api-client";
import { isFinished, relativeTime, shortHash } from "@/lib/format";
import { requireSession } from "@/lib/session";
import { LiveRefresh } from "./LiveRefresh";
import { RunControls } from "./RunControls";

export const dynamic = "force-dynamic";

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const { client, me } = await requireSession();
  let run;
  try {
    run = await client.getRun(id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }
  const finished = isFinished(run.status);
  const canControl = me.user_id === run.acting_user_id || me.roles.includes("admin");
  const waiting = run.steps.some((step) => step.status === "awaiting_approval");
  return (
    <Shell me={me}>
      <LiveRefresh runId={run.run_id} finished={finished} />
      <p><Link href="/runs">← Runs</Link></p>
      <h1>{run.objective}</h1>
      <p className="row">
        <StatusBadge status={run.status} />
        <span className="muted">
          {run.profile} · started by {run.acting_user_id} {relativeTime(run.created_at)} · plan v{run.plan_version}
        </span>
      </p>
      {run.error && <p className="notice error">{run.error}</p>}
      {waiting && (
        <p className="notice wait">
          A step is waiting for approval. <Link href="/approvals">Open the approval inbox</Link> as someone
          with the required role.
        </p>
      )}
      {canControl && !finished && <RunControls runId={run.run_id} />}
      <h2>Steps</h2>
      {run.steps.length === 0 ? (
        <p className="muted">Planning… no steps yet.</p>
      ) : (
        <table>
          <thead>
            <tr><th>Step</th><th>Tool</th><th>Status</th><th>Policy</th><th>Needs</th><th>Action hash</th></tr>
          </thead>
          <tbody>
            {run.steps.map((step) => (
              <tr key={step.step_id}>
                <td>
                  {step.step_id}
                  {step.depends_on.length > 0 && <div className="muted">after {step.depends_on.join(", ")}</div>}
                </td>
                <td><code>{step.tool}</code></td>
                <td>
                  <StatusBadge status={step.status} />
                  {step.error && <div className="muted">{step.error}</div>}
                </td>
                <td>
                  <StatusBadge status={step.policy_outcome} />
                  {step.policy_rule_ids.length > 0 && <div className="muted">{step.policy_rule_ids.join(", ")}</div>}
                </td>
                <td>{step.approver_role ?? "-"}</td>
                <td><code title={step.action_hash ?? ""}>{shortHash(step.action_hash)}</code></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p><Link href={`/runs/${encodeURIComponent(run.run_id)}/audit`}>Audit trail and verification →</Link></p>
    </Shell>
  );
}
