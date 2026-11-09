"use client";
import { useActionState } from "react";
import { login, type ActionState } from "@/app/actions";
import { Notice } from "@/components/Notice";
import { SubmitButton } from "@/components/SubmitButton";

export function LoginForm() {
  const [state, action] = useActionState<ActionState, FormData>(login, null);
  return (
    <form action={action} className="stack">
      <label>
        Token
        <input name="token" type="password" autoComplete="off" required />
      </label>
      <Notice state={state} />
      <SubmitButton>Sign in</SubmitButton>
    </form>
  );
}
