import { demoSignIn } from "./actions";

export function SignInAs({ person, next, label }: { person: string; next: string; label: string }) {
  return (
    <form action={demoSignIn} className="inline">
      <input type="hidden" name="person" value={person} />
      <input type="hidden" name="next" value={next} />
      <button type="submit" className="link">{label}</button>
    </form>
  );
}
