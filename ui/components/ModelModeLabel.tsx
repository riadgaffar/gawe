import { modelLabel } from "@/lib/demo-view";

// M29: shown wherever a run's model is shown, so a scripted run is never mistaken for a real
// model's judgement.
export function ModelModeLabel({ mode }: { mode: string | null | undefined }) {
  const label = modelLabel(mode);
  if (!label) return null;
  return (
    <p className="notice" role="note">
      <strong>{label}.</strong> This run's model answers from a fixed demo script, not a real model.
    </p>
  );
}
