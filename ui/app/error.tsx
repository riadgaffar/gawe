"use client";

export default function ErrorPage({ reset }: { error: Error; reset: () => void }) {
  return (
    <main>
      <h1>Something went wrong</h1>
      <p>
        The page could not load. Check that the API is running (<code>make api</code>) and that{" "}
        <code>GAWE_API_URL</code> points at it.
      </p>
      <button type="button" onClick={reset}>Try again</button>
    </main>
  );
}
