"use client";
import { useFormStatus } from "react-dom";

export function SubmitButton(props: {
  children: React.ReactNode;
  name?: string;
  value?: string;
  className?: string;
  disabled?: boolean;
}) {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      name={props.name}
      value={props.value}
      className={props.className}
      disabled={pending || props.disabled}
    >
      {pending ? "Working…" : props.children}
    </button>
  );
}
