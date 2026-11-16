import { Shell } from "@/components/Shell";
import { requireSession } from "@/lib/session";
import { ReviewItemCard } from "./ReviewItemCard";

export const dynamic = "force-dynamic";

export default async function ReviewPage() {
  const { client, me } = await requireSession();
  if (!me.roles.includes("legal_reviewer")) {
    return (
      <Shell me={me}>
        <h1>Next-day review</h1>
        <p>Reviewing needs the legal_reviewer role.</p>
      </Shell>
    );
  }
  const items = await client.reviewItems();
  return (
    <Shell me={me}>
      <h1>Next-day review</h1>
      <p className="muted">
        Yesterday&apos;s automatic decisions that need a second look, and a keyed sample of low
        ratings. You cannot give a verdict on a run you started.
      </p>
      {items.length === 0 ? (
        <p>Nothing open.</p>
      ) : (
        items.map((item) => <ReviewItemCard key={item.item_id} item={item} />)
      )}
    </Shell>
  );
}
