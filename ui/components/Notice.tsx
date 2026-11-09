import type { ActionState } from "@/app/actions";

export function Notice({ state }: { state: ActionState }) {
  if (!state) return null;
  if (state.error) return <p className="notice error" role="alert">{state.error}</p>;
  if (state.ok) return <p className="notice ok" role="status">{state.ok}</p>;
  return null;
}
