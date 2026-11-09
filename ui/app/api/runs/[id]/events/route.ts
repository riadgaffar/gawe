// Streams one run's audit events to the browser. The only API path the browser reaches directly;
// the token is added here from the cookie and the browser never sees it.
import { apiBase, getToken } from "@/lib/session";

export const dynamic = "force-dynamic";

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const token = await getToken();
  if (!token) return new Response("unauthorized", { status: 401 });
  const { id } = await params;
  const lastEventId = request.headers.get("last-event-id");
  let upstream: Response;
  try {
    upstream = await fetch(`${apiBase()}/v1/runs/${encodeURIComponent(id)}/events`, {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "text/event-stream",
        ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
      },
      cache: "no-store",
      signal: request.signal,
    });
  } catch {
    return new Response("API unreachable", { status: 502 });
  }
  if (!upstream.ok || !upstream.body) {
    return new Response("upstream error", { status: upstream.status === 404 ? 404 : 502 });
  }
  return new Response(upstream.body, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-store, no-transform",
      "X-Accel-Buffering": "no",
    },
  });
}
