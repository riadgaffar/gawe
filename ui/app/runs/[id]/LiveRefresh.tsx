"use client";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

/** Re-renders the page when the run's audit stream reports something new. Renders nothing. */
export function LiveRefresh({ runId, finished }: { runId: string; finished: boolean }) {
  const router = useRouter();
  useEffect(() => {
    if (finished) return;
    const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = () => {
      clearTimeout(timer);
      timer = setTimeout(() => router.refresh(), 150); // coalesce bursts of events
    };
    source.addEventListener("audit", refresh);
    source.addEventListener("end", () => {
      source.close();
      refresh();
    });
    return () => {
      clearTimeout(timer);
      source.close();
    };
  }, [runId, finished, router]);
  return null;
}
