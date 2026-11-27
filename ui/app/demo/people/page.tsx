import { notFound } from "next/navigation";
import { demoGate } from "@/lib/demo";
import { SignInAs } from "../SignInAs";

export const dynamic = "force-dynamic";

// M29: one-click sign-in as the demo's people. Only in demo mode on localhost (see demoGate).
export default async function DemoPeoplePage() {
  const info = await demoGate();
  if (!info) notFound();
  return (
    <main className="narrow stack">
      <h1>Sign in as someone in the demo</h1>
      <p className="muted">
        Three imaginary companies run the same contract review agent with different settings.
        Pick a person; the <a href="/demo">Demo page</a> lists what to try.
      </p>
      {info.companies.map((company) => (
        <section key={company.key} className="card stack">
          <h2>{company.name}</h2>
          <p className="muted">{company.summary}</p>
          <ul>
            {company.people.map((person) => (
              <li key={person.key}>
                <SignInAs person={person.key} next="/runs" label={`${person.name}, ${person.title}`} />
              </li>
            ))}
          </ul>
        </section>
      ))}
    </main>
  );
}
