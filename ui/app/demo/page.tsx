import { notFound } from "next/navigation";
import { demoGate } from "@/lib/demo";
import { personFor } from "@/lib/demo-view";
import { SignInAs } from "./SignInAs";
import { StartSceneForm } from "./StartSceneForm";

export const dynamic = "force-dynamic";

// M29: the guided demo. Only in demo mode on localhost (see demoGate).
export default async function DemoPage() {
  const info = await demoGate();
  if (!info) notFound();
  const careful = info.companies.find((c) => c.key === info.fixture?.company);
  const carefulAlice = careful ? personFor(careful, "case_manager") : null;
  return (
    <main className="stack">
      <h1>The GAWE demo</h1>
      <p>
        An agent reviews contracts for three imaginary companies. The code is the same; only each
        company's settings differ. The model is <strong>scripted</strong>: it answers from a fixed
        script, honestly or as if fooled by the document, so you can watch what policy does either
        way. It shows what happens when a model is fooled, not how often a real one is.
      </p>
      <p className="muted">
        Start a scene as the company's case manager, then decide what waits as its reviewer or
        approver. Or <a href="/demo/people">sign in as anyone</a>.
      </p>
      <section className="card stack">
        <h2>The companies</h2>
        <ul>
          {info.companies.map((company) => (
            <li key={company.key}>
              <strong>{company.name}</strong>: {company.summary}
            </li>
          ))}
        </ul>
        <p className="muted">
          Big-Deals turned off the rule that sends every low rating to legal. That is a decision
          with a cost: a fooled low rating on a small deal goes through there. GAWE enforces what
          each company chose.
        </p>
      </section>
      {info.scenes.map((scene) => (
        <section key={scene.scene_id} className="card stack">
          <h2>{scene.title}</h2>
          <p><span className="muted">Objective:</span> {scene.objective}</p>
          <p><span className="muted">What to expect:</span> {scene.expect}</p>
          {scene.fooled_how && <p><span className="muted">Fooled:</span> {scene.fooled_how}.</p>}
          <table>
            <tbody>
              {info.companies.map((company) => {
                const legal = personFor(company, "legal_reviewer");
                const finance = personFor(company, "finance_approver");
                return (
                  <tr key={company.key}>
                    <th scope="row">{company.name}</th>
                    <td>
                      {scene.modes.map((mode) => (
                        <StartSceneForm
                          key={mode}
                          company={company.key}
                          scene={scene.scene_id}
                          mode={mode}
                          label={mode === "honest" ? "Start (honest model)" : "Start (fooled model)"}
                        />
                      ))}
                    </td>
                    <td>
                      {legal && <SignInAs person={legal.key} next="/approvals" label={`Decide as ${legal.name} (legal)`} />}{" "}
                      {finance && <SignInAs person={finance.key} next="/approvals" label={`Decide as ${finance.name} (finance)`} />}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      ))}
      {info.fixture && carefulAlice && (
        <section className="card stack">
          <h2>A tampered audit trail</h2>
          <p>
            A <strong>pre-built fixture</strong>: an ordinary run whose stored audit trail had one
            event altered before it was loaded. Open its audit trail and click Verify: GAWE finds
            the break.
          </p>
          <SignInAs
            person={carefulAlice.key}
            next={`/runs/${encodeURIComponent(info.fixture.run_id)}/audit`}
            label={`See it as ${carefulAlice.name} (${careful?.name})`}
          />
        </section>
      )}
    </main>
  );
}
