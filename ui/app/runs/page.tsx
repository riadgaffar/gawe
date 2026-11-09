import Link from "next/link";
import { Shell } from "@/components/Shell";
import { StatusBadge } from "@/components/StatusBadge";
import { relativeTime } from "@/lib/format";
import { requireSession } from "@/lib/session";
import { StartRunForm } from "./StartRunForm";

export const dynamic = "force-dynamic";

export default async function RunsPage() {
  const { client, me } = await requireSession();
  const runs = await client.listRuns();
  return (
    <Shell me={me}>
      <h1>Runs</h1>
      <StartRunForm />
      <h2>Recent runs</h2>
      {runs.length === 0 ? (
        <p className="muted">No runs yet.</p>
      ) : (
        <table>
          <thead>
            <tr><th>Objective</th><th>Status</th><th>Started by</th><th>Started</th></tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.run_id}>
                <td><Link href={`/runs/${encodeURIComponent(run.run_id)}`}>{run.objective}</Link></td>
                <td><StatusBadge status={run.status} /></td>
                <td>{run.acting_user_id}</td>
                <td>{relativeTime(run.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Shell>
  );
}
