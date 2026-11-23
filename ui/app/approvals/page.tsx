import { Shell } from "@/components/Shell";
import { requireSession } from "@/lib/session";
import { ApprovalCard } from "./ApprovalCard";
import { HandoffCard } from "./HandoffCard";

export const dynamic = "force-dynamic";

export default async function ApprovalsPage() {
  const { client, me } = await requireSession();
  const [approvals, handoffs] = await Promise.all([client.inbox(), client.handoffs()]);
  return (
    <Shell me={me}>
      <h1>Approval inbox</h1>
      <p className="muted">
        Only actions your role may decide, requested by someone else, still waiting. Reload to see new ones.
      </p>
      {approvals.length === 0 ? (
        <p>Nothing waiting for you.</p>
      ) : (
        approvals.map((approval) => <ApprovalCard key={approval.approval_id} approval={approval} />)
      )}
      <h2>Hand-offs</h2>
      <p className="muted">
        Actions GAWE only prepares: your role carries them out by hand, then marks each one done or not done.
      </p>
      {handoffs.length === 0 ? (
        <p>No hand-offs waiting for you.</p>
      ) : (
        handoffs.map((handoff) => (
          <HandoffCard key={`${handoff.run_id}/${handoff.step_id}`} handoff={handoff} />
        ))
      )}
    </Shell>
  );
}
