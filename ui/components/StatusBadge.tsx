import { label, toneFor } from "@/lib/format";

export function StatusBadge({ status }: { status: string | null }) {
  if (!status) return <span className="muted">-</span>;
  return <span className={`badge tone-${toneFor(status)}`}>{label(status)}</span>;
}
