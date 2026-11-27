# M29 design: A packaged demo

Status: **Approved 2026-11-26** (D1–D8 as drafted, each with the recommended option). Nothing is
built. No paid runs are needed.

## 1. Purpose and boundaries

The queue of work after M13 is done: every item is built, decided, or waiting for a real system.
Next comes a packaged demo.
Anyone who clones the repo runs one command and watches GAWE stop an agent from doing the wrong
thing, even when the model is fooled, without an API key or any setup. Several imaginary
companies show that the same agent ends differently under each company's settings, with no new
code. The owner has said the demo goes in the public repo.

In scope:
- one command up and one down;
- a scripted model the API can use, honest or fooled per run, labelled as such;
- per-company settings for contract review, and three seeded companies;
- one-click sign-in as the demo's people;
- a guided demo page in the UI;
- a headless check that plays every scene;
- docs.

Out of scope:
- **hosting:** a public instance, anonymous visitors and abuse controls (the scoping note's
  slices 2 and 3);
- **Docker images for the API and UI;**
- **the other two apps** (IT access and event replay stay in `docs/demo-script.md`);
- **self-service configuration** (the next product candidate);
- **real connectors.**

## 2. What the codebase does today

- **Settings are fixed in code for every tenant.** Contract review's `TENANT_VARIABLES` (internal
  and blocked domains) is one constant, so every company sees the same values. Two of the demo's
  differences aren't settings at all:
  - "every low needs legal" (`low-risk-needs-legal`) is unconditional;
  - the large-deal line is the constant `LARGE_DEAL_THRESHOLD` (100,000).

  The signature rules' 100,000 is a literal in `policies.yaml`.
- **The API can only use the real model.** `create_app_from_env` always builds
  `AnthropicProvider`. The benchmarks' `ScriptedModel` answers every call with one fixed reply
  per case. It can't tell which document it was given.
- **Memory stores work end to end.** `GAWE_STORE=memory` needs no Postgres and no Docker, and
  every feature the tests cover runs on it. It forgets everything when the API stops.
- **Sign-in is a pasted token,** one per person, from a token file, each tied to one tenant. Three
  companies with three people each is nine tokens to paste.
- **The tamper scene needs database-owner access** (an SQL edit in the Postgres container). With
  memory stores there is nothing to edit from outside.
- **The mock contract system is shared by all tenants.** Every company sees the same contracts,
  which suits "same document, different outcome".
- `make demo` exists, but it needs Docker and Postgres, skips `app-role`, and starts the API
  before the model key is in `.env`.

## 3. Decisions for the owner

### D1. How the demo runs

| Option | Needs | Assessment |
|---|---|---|
| **A. `make demo`: memory stores, scripted model, API and UI started together (recommended)** | Python and Node; no Docker, no key | Fewest moving parts. Nothing persists: stopping is the reset |
| B. The same on Postgres | Docker too | Runs persist, and the real tamper scene works. It brings back the port and password problems newcomers hit |
| C. `docker compose` with images for the API and UI | Docker only | Best for "clone and run", and the base a hosted version needs. Images and a compose setup are new work: a follow-up |

With A, `make demo` starts the API and the UI and stops both on Ctrl-C. `make demo DEMO_STORE=postgres`
keeps option B available for the tamper scene.

### D2. The scripted model

A `DemoModel` provider, chosen by `GAWE_LLM_PROVIDER=demo`. It recognises each demo document by
its hash and answers the Reader and the Planner with that scene's script. A document it doesn't
know is refused with a clear error. It never guesses.

| Option | How honest or fooled is chosen | Assessment |
|---|---|---|
| **A. Per run (recommended)** | the run is started with `model_mode: honest` or `fooled`; accepted only when the API runs the demo provider, refused otherwise, and stored on the run | Both versions of a scene run side by side under one API. The run page, the approval card and the audit say "scripted demo model: fooled" |
| B. Per process | an environment setting; switching means restarting the API | Simpler, but the demo page can't offer "the same contract, model fooled" next to the honest run |

"Fooled" reuses the scripted benchmark's techniques: the Reader reports what the injected text
wants, or the Planner proposes the attacker's step. The mode only chooses the scripted replies. It
changes no policy, approval or control.

### D3. Per-company settings

| Option | Where they live | Assessment |
|---|---|---|
| **A. A settings file per tenant, read at startup (recommended)** | `GAWE_TENANT_SETTINGS` names a file mapping tenant ids to settings. It is validated against the app's settings schema, frozen in the bundle, and a tenant not in it gets today's values | No route or tool can change it (M23 holds). Startup refuses an unknown key or a wrong type |
| B. A table with an admin page | the database | That is the next product candidate, self-service configuration, with versions and approval. Too much for a demo |

New contract review settings, with today's values as the defaults:

| Setting | Default (today) | What it changes |
|---|---|---|
| `review_levels_always` | `["low"]` | the levels that always need legal; `low-risk-needs-legal` reads it |
| `large_deal_threshold` | 100,000 | the line for "low on a large deal needs legal" |
| `internal_domains`, `blocked_domains` | today's lists | already settings; now per tenant |

The signature rules' 100,000 stays a literal, to keep the change small.

With defaults equal to today's values, the default tenant and every benchmark see the same
decisions. Scripted 38, integrity 23, undo 4, replay 9 and record changes 18 should stay identical.
The boundary oracle and the unreadable-input suite gain the new variables.

### D4. The three companies

| Company | `review_levels_always` | `large_deal_threshold` | `blocked_domains` |
|---|---|---|---|
| Careful Co. | `["low"]` | 100,000 | `evil.example` |
| Big-Deals Co. | `[]` | 250,000 | `evil.example` |
| Rivals Co. | `["low"]` | 100,000 | `evil.example`, `competitor.example` |

What the same scene does at each:

| Scene | Careful | Big-Deals | Rivals |
|---|---|---|---|
| C-1001 (45,000), honest "low" | waits for legal | runs | waits for legal |
| C-1003 (80,000), Reader fooled into "low" | waits for legal | **runs** | waits for legal |
| C-1003, Planner fooled into emailing `attacker@evil.example` | denied | denied | denied |
| C-1001, email a summary to `someone@competitor.example` | waits for legal (external) | waits for legal | denied |
| C-1002 (250,000), send for signature | finance | finance | finance |

The "runs" results assume the contract has no earlier, higher rating in that company. Otherwise
M24's `rating-lowered-needs-legal` holds the lower rating for legal everywhere. The demo seeds
each company fresh, so a scene's first run shows the setting itself.

The second row is deliberate. Big-Deals turned off the rule M17 kept, and a fooled "low" on a
small deal goes through there. The demo page says so plainly: a setting is a decision with a
cost, and GAWE enforces what the company chose. The default stays as M17 decided.

### D5. Signing in as the demo's people

| Option | How | Assessment |
|---|---|---|
| **A. A demo sign-in page (recommended)** | `make demo` makes tokens for the nine people into a local file only the UI's server reads; the sign-in page lists them by company and role, one click each | It exists only when the UI runs with `GAWE_UI_DEMO=1` and the API with the demo provider, and only for localhost. Ordinary sign-in is unchanged |
| B. Paste tokens | as today | Nine tokens to manage; the friction the demo is meant to remove |

### D6. Guidance

| Option | What | Assessment |
|---|---|---|
| **A. A Demo page in the UI (recommended)** | lists the scenes by company. Each has buttons such as "start as Alice", "decide as Bob" and "see the audit", and says what to expect and why | New UI with its own model and rendered tests (M28's harness) |
| B. Docs only | an updated `docs/demo-script.md` | Less work; the copying and token switching stay |

Both keep `docs/demo-script.md` as the full reference.

### D7. The tamper scene

With memory stores, a run's audit trail can't be edited from outside. Options:
- **A. A pre-built tampered run (recommended):** seeded at start, labelled as a pre-built fixture.
  Verify reports "Chain BROKEN at event N". The real Postgres edit stays documented for
  `DEMO_STORE=postgres`.
- **B. Leave it to `DEMO_STORE=postgres` only.**

Neither adds a code path that edits a stored audit event.

### D8. A headless check

`python -m bench demo` plays every scene of D4 under the demo provider in memory, with no UI, and
compares each with an expected record written by hand, like `bench integrity`. Tests run it, so a
change that breaks the demo fails the checks.

## 4. What changes, if approved

- **Engine:**
  - a provider choice in `create_app_from_env` (`anthropic` by default, `demo`);
  - an optional `model_mode` on starting a run, refused unless the demo provider is in use;
  - a per-tenant settings hook for apps.
- **Contract review:** the two new settings, `low-risk-needs-legal` and the large-deal variable
  reading them, and defaults equal to today.
- **Demo package (`gawe.demo`):**
  - the `DemoModel` and its scene scripts;
  - the three companies' settings file;
  - the people and tokens;
  - the pre-built tampered run.
- **UI:**
  - a model-mode banner on runs, approvals and the audit page;
  - the demo sign-in page;
  - the Demo page.
- **Make:**
  - `make demo` starts both processes;
  - `DEMO_STORE=postgres` for the Postgres variant.
- **Tests:**
  - the provider and its refusals;
  - settings validation and defaults;
  - the companies' outcomes;
  - the sign-in page's gating;
  - the Demo page (model and rendered);
  - `bench demo`;
  - the boundary and unreadable-input suites.
- **Docs:**
  - the README leads with `make demo`;
  - `docs/demo-script.md` starts from it;
  - roadmap, threat model and API docs.

## 5. Expected changes to existing numbers

None: today's values are the defaults, and the demo's companies are new tenants. Each moved
record, if any, is reported first.

## 6. Limits

- Scripted, not measured: the demo shows what policy does when a model is honest or fooled, not
  how often a real model is fooled. That is the benchmark's job.
- Synthetic data and mock systems only. The demo sign-in is for localhost.
- Memory mode forgets everything on stop. The tamper scene there is a labelled fixture.
- Contract review only. The other apps keep their walkthroughs in `docs/demo-script.md`.
- Per-tenant settings come from a file read at startup. Changing them means a restart, and
  nothing in GAWE can change them.

## 7. Approval

Approved by the owner on 2026-11-26: D1–D8 as drafted, each with the recommended option, the
name M29, the demo in the public repo, and no paid runs. The build plan is in `docs/m29-plan.md`, approved the same day.

## 8. Build record

Built 2026-11-26 to 2026-11-27 on `packaged-demo` in five steps (S1–S5 in `docs/m29-plan.md`,
which records each step's details), D1–D8 as approved, no paid runs. Committed by the owner
and merged to `main` on 2026-11-27.

- **D1, one command:** `make demo` (memory stores) and `make demo DEMO_STORE=postgres` (its own
  `gawe_demo` database). Both servers listen on 127.0.0.1; Ctrl-C stops both.
- **D2, the scripted model:** `GAWE_LLM_PROVIDER=demo`, honest or fooled per run
  (`model_mode`, migration 0012), refused under a real model; labelled on the run, the approval
  card and the audit trail. It answers only its scenes' objectives and documents.
- **D3, per-company settings:** `GAWE_TENANT_SETTINGS`, checked and frozen at startup; with no
  file, every decision is as before.
- **D4, the companies:** Careful, Big-Deals and Rivals; the table's outcomes are checked case by
  case by `bench demo` (24 of 24).
- **D5–D6, sign-in and guidance:** `/demo/people` and the `/demo` page, gated by the UI's flag,
  the API's demo mode and a local request.
- **D7, tamper:** a pre-built fixture on memory stores (`Chain BROKEN at event 3`), the real
  edit on Postgres.
- **D8, headless:** `python -m bench demo`.

At the end: `make check` 3757 passed, 101 skipped; UI 77 model and 26 rendered tests and the
build; Postgres 157; check-docs 0 problems; every existing benchmark group identical to M28
(section 5's expectation held); a negative matrix of ten mutations, each caught. Settled during
the build, all recorded in the plan:
- a settings file holds one app's tenants;
- `/v1/demo` needs no sign-in and never returns a token;
- the Postgres demo uses its own database;
- Next's `AGENTS.md` generation is turned off;
- the launcher restores `ui/next-env.d.ts`.

The limits in section 6 stand.
