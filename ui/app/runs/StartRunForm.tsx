"use client";
import { useActionState, useEffect, useState } from "react";
import { startRun, type ActionState } from "@/app/actions";
import { Notice } from "@/components/Notice";
import { SubmitButton } from "@/components/SubmitButton";

type Sample = { file: string; profile: string; objective: string; title: string };

export function StartRunForm() {
  const [state, action] = useActionState<ActionState, FormData>(startRun, null);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [objective, setObjective] = useState("");
  const [profile, setProfile] = useState("contract_review");
  const [docText, setDocText] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/samples/index.json")
      .then((r) => (r.ok ? r.json() : []))
      .then((list: Sample[]) => setSamples(list))
      .catch(() => setSamples([]));
  }, []);

  async function pick(file: string) {
    const sample = samples.find((s) => s.file === file);
    if (!sample) return;
    setLoadError(null);
    try {
      const response = await fetch(`/samples/${encodeURIComponent(sample.file)}`);
      if (!response.ok) throw new Error("not found");
      setDocText(await response.text());
      setObjective(sample.objective);
      setProfile(sample.profile);
    } catch {
      setLoadError("Could not load that sample.");
    }
  }

  return (
    <form action={action} className="stack card">
      <h2>Start a run</h2>
      {samples.length > 0 && (
        <label>
          Use a synthetic sample
          <select defaultValue="" onChange={(e) => void pick(e.target.value)}>
            <option value="">Choose…</option>
            {samples.map((s) => (
              <option key={s.file} value={s.file}>{s.title}</option>
            ))}
          </select>
        </label>
      )}
      {loadError && <p className="notice error">{loadError}</p>}
      <label>
        Objective
        <textarea name="objective" rows={3} required value={objective} onChange={(e) => setObjective(e.target.value)} />
      </label>
      <label>
        Profile
        <input name="profile" required value={profile} onChange={(e) => setProfile(e.target.value)} />
      </label>
      <label>
        Document (text or Markdown)
        <textarea name="document" rows={8} value={docText} onChange={(e) => setDocText(e.target.value)} />
      </label>
      <label>
        …or upload a .txt / .md file (replaces the text above)
        <input name="file" type="file" accept=".txt,.md,text/plain,text/markdown" />
      </label>
      <Notice state={state} />
      <SubmitButton>Start run</SubmitButton>
    </form>
  );
}
