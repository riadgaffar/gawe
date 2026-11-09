import Link from "next/link";
import { notFound } from "next/navigation";
import { Shell } from "@/components/Shell";
import { ApiError } from "@/lib/api-client";
import { shortHash } from "@/lib/format";
import { requireSession } from "@/lib/session";
import { VerifyButton } from "./VerifyButton";

export const dynamic = "force-dynamic";

export default async function AuditPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const { client, me } = await requireSession();
  let events;
  try {
    events = await client.audit(id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }
  return (
    <Shell me={me}>
      <p><Link href={`/runs/${encodeURIComponent(id)}`}>← Run</Link></p>
      <h1>Audit trail</h1>
      <p className="muted">
        Each event stores the hash of the one before it. Verifying recomputes the whole chain, so an
        edited or deleted row is detected.
      </p>
      <VerifyButton runId={id} />
      {events.length === 0 ? (
        <p className="muted">No events.</p>
      ) : (
        <table>
          <thead>
            <tr><th>#</th><th>Event</th><th>Actor</th><th>Payload hash</th><th>Chain hash</th><th>Time</th></tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr key={event.seq}>
                <td>{event.seq}</td>
                <td><code>{event.type}</code></td>
                <td>{event.actor}</td>
                <td><code title={event.payload_hash}>{shortHash(event.payload_hash)}</code></td>
                <td><code title={event.hash}>{shortHash(event.hash)}</code></td>
                <td>{new Date(event.ts).toISOString().replace("T", " ").slice(0, 19)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Shell>
  );
}
