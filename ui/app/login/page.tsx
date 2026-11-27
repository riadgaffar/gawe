import { demoGate } from "@/lib/demo";
import { LoginForm } from "./LoginForm";

export const dynamic = "force-dynamic";

export default async function LoginPage() {
  const demo = await demoGate();
  return (
    <main className="narrow">
      <h1>GAWE</h1>
      <p className="muted">
        Paste a dev token (printed by <code>make dev-tokens</code>). The token is kept in an
        httpOnly cookie and is never shown on screen again.
      </p>
      <LoginForm />
      {demo && (
        <p>
          Running the demo? <a href="/demo">Open the Demo page</a> or{" "}
          <a href="/demo/people">sign in as one of its people</a>.
        </p>
      )}
    </main>
  );
}
