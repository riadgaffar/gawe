import Link from "next/link";
import { logout } from "@/app/actions";
import type { MeView } from "@/lib/api-types";

export function Shell({ me, children }: { me: MeView; children: React.ReactNode }) {
  return (
    <>
      <header className="nav">
        <strong>GAWE</strong>
        <Link href="/runs">Runs</Link>
        <Link href="/approvals">Approvals</Link>
        <span className="spacer" />
        <span className="muted">
          {me.user_id} · {me.roles.join(", ") || "no roles"}
        </span>
        <form action={logout}>
          <button type="submit" className="link">Sign out</button>
        </form>
      </header>
      <main>{children}</main>
    </>
  );
}
