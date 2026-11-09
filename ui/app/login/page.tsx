import { LoginForm } from "./LoginForm";

export default function LoginPage() {
  return (
    <main className="narrow">
      <h1>GAWE</h1>
      <p className="muted">
        Paste a dev token (printed by <code>make dev-tokens</code>). The token is kept in an
        httpOnly cookie and is never shown on screen again.
      </p>
      <LoginForm />
    </main>
  );
}
