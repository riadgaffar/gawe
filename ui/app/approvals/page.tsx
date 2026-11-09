import { Shell } from "@/components/Shell";
import { requireSession } from "@/lib/session";
import { ApprovalCard } from "./ApprovalCard";

export const dynamic = "force-dynamic";

export default async function ApprovalsPage() {
  const { client, me } = await requireSession();
  const approvals = await client.inbox();
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
    </Shell>
  );
}
