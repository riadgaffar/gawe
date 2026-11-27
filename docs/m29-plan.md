# M29 build plan

Status: **Phase 4.** Plan approved by the owner on 2026-11-26, with its one migration. Nothing is
built yet. It implements D1–D8 in
[m29-design.md](m29-design.md), approved by the owner on 2026-11-26, each with the recommended
option. No paid model calls.

## Working rules and verification

Work on the branch `packaged-demo`. Do not commit, stage or push; the owner reviews and commits
each checkpoint. No live model calls and no real connectors.

**One migration is planned** (S2: a nullable `model_mode` column on `runs`). Approving this plan
approves that migration. It is tested with `make test-schema` and an Alembic upgrade and downgrade
on a throwaway database. Any other migration or engine change beyond what's listed stops for the
owner first.

Run S1–S5 in order and stop for owner review after each, with:
- the diff summary;
- the verification output, with a log in an ignored `bench-results/m29-sN-*.txt`;
- known limitations;
- a suggested one-line commit message.

If verification fails twice on the same step, stop and report. A change to approved semantics
needs a new owner decision; routine details may be settled here and recorded.

At each checkpoint run:
- `make check` and `make check-docs`;
- every benchmark group with identical records: `bench scripted` (38), `bench integrity` (23 and
  undo 4), `bench replay` (9), `bench record-changes` (18). A step that moves one reports it first;
- `make test-integration` when Postgres code changes, and `make ui-check` when the UI changes.

## S1. Per-company settings (D3)

- **The hook:** `AppBundle` gains a settings schema and defaults.
  - A `GAWE_TENANT_SETTINGS` file (JSON) maps tenant ids to settings.
  - At startup it is validated against the app's schema: an unknown key, a wrong type or an
    unknown app is refused.
  - Each tenant's settings are merged over the defaults and frozen in the bundle.
  - An app's variables read the run's tenant's settings. A tenant not in the file gets the
    defaults.
  - No route, tool or maintenance command can change them (the M23 inventory tests keep pinning
    that).
- **Contract review** gains `review_levels_always` (default `["low"]`) and
  `large_deal_threshold` (default 100,000), and `internal_domains` and `blocked_domains` become
  per tenant.
  - `low-risk-needs-legal` reads `risk_level: {in: $tenant.review_levels_always}`.
  - The large-deal variable, including M27's captured variables, uses the tenant's threshold.
  - The other two apps keep their settings but use the same hook, so a file can set them per
    tenant too.

Tests:
- with no file, every decision is as today: golden cases, scripted, integrity, replay and record
  changes identical;
- a file gives two tenants different outcomes for the same action (the low rating, the large
  deal, the blocked domain);
- a tenant not in the file gets the defaults;
- a bad file stops startup (unknown key, wrong type, an empty list where one is required);
- a missing setting denies (unreadable never allows);
- the boundary oracle and registry, and the unreadable-input targets, gain the new variables.

Suggested commit: `feat(policy): settings per tenant from a file read at startup`

## S2. The scripted demo model and a run's model mode (D2)

- **`gawe.demo.model.DemoModel`:** a provider answering the Reader and the Planner from scene
  scripts, keyed by the SHA-256 of the demo document. It has an honest and a fooled script per
  document; a document it doesn't know is refused, so the run fails at reading with a clear code.
  The fooled scripts reuse the scripted benchmark's techniques (the Reader reporting what the
  injected text wants; the Planner proposing the attacker's step).
- **Choosing it:** `GAWE_LLM_PROVIDER` is `anthropic` (the default) or `demo`.
- **`model_mode`:** starting a run takes an optional `model_mode` (`honest` or `fooled`).
  - It's refused with 400 unless the API runs the demo provider.
  - It's stored on the run: `RunRecord.model_mode`, plus the migration's nullable column.
  - The orchestrator passes it to the provider with each request.
  - It appears in the run view, the approval view and the `run.created` audit event.
  - The model name the run records says "scripted demo model".

Tests:
- the provider's scripts and its refusal of unknown documents;
- `model_mode` refused under the real provider and accepted under the demo provider;
- honest and fooled runs of one document side by side, with different plans and the same
  policy;
- the mode is in the views and the audit, and stored and read back in Postgres;
- `make test-schema`, and an Alembic upgrade and downgrade on a throwaway database.

Suggested commit: `feat(demo): a scripted demo model, honest or fooled per run`

## S3. The companies, the people, the tamper fixture and `bench demo` (D4, D7, D8)

- **`gawe.demo`:**
  - the three companies' settings (Careful, Big-Deals, Rivals), as D4 sets them;
  - their people (a case manager, a legal reviewer and a finance approver each), with tokens
    generated into an ignored file at start;
  - the scenes of D4, each naming its company, document, objective, mode and who starts and
    decides.
- **The tamper fixture:** in demo mode on memory stores, one pre-built run whose stored chain
  was built broken at a known event, labelled as a fixture. Verify reports `Chain BROKEN at
  event N`. Nothing edits a stored event: the fixture's events are written once, as built.
- **`python -m bench demo`:** plays every scene under the demo provider in memory, with no UI and
  no model, and compares each with an expected record written by hand. It writes its own run
  directory and exits 1 on a difference.

Tests:
- every scene's record;
- the D4 table, company by company;
- the fixture is present only in demo mode and verifies as broken at event N;
- the tokens file is never written outside demo mode;
- `bench demo`'s command and a changed outcome being reported.

Suggested commit: `feat(demo): three companies, their people, the scenes and bench demo`

## S4. The UI: model label, demo sign-in and the Demo page (D5, D6)

- **A model-mode label** on the run page, the approval card and the audit page:
  "Scripted demo model: honest" or "…: fooled".
- **The demo sign-in page:**
  - it lists the demo's people by company and role, one click each;
  - the UI's server reads the demo tokens file;
  - it exists only when the UI runs with `GAWE_UI_DEMO=1`, the API reports the demo provider,
    and the request comes from localhost;
  - otherwise it returns not found;
  - ordinary sign-in is unchanged.
- **The Demo page:**
  - the scenes by company, each saying what to expect and why;
  - buttons: "start as …" starts the run with that person's token and mode, "decide as …"
    opens the approvals page signed in as the approver, and "see the audit";
  - a short line on each company's settings;
  - the Big-Deals note that a setting is a decision with a cost.
- **API:** a read-only `GET /v1/demo` (scenes and whether demo mode is on), added to M23's pinned
  route list.

Tests:
- card and page models;
- rendered tests (M28's harness) for the label, the sign-in page's people and the Demo page's
  scenes;
- the gating: no flag, a non-local request or a real-model API each give not found.

Suggested commit: `feat(ui): demo sign-in, a Demo page and the scripted-model label`

## S5. One command, docs and verification (D1)

- **`make demo`:**
  - checks Python and Node;
  - starts the API (memory stores, demo provider, the companies' settings) and the UI
    (`GAWE_UI_DEMO=1`);
  - waits for the API to answer, prints the URL and opens nothing on its own;
  - Ctrl-C stops both.
- **`make demo DEMO_STORE=postgres`:** the same on Postgres. It needs Docker, sets up the database
  and keeps the real tamper scene documented.
- **The old `make demo`** (Postgres and the real model) is replaced; the step-by-step setup in
  `docs/demo-script.md` stays for the real model.
- **Docs:**
  - the README leads with `make demo`;
  - `docs/demo-script.md` starts from it;
  - the API docs cover `model_mode`, `GAWE_LLM_PROVIDER`, `GAWE_TENANT_SETTINGS` and
    `/v1/demo`;
  - the threat model: the demo sign-in's gating, the scripted label, and settings that no route
    can change;
  - the roadmap: the M29 row;
  - both completion records.
- **Negative matrix** on exported copies, each mutation with a test that fails when its guard is
  removed, after a passing control run:
  - `model_mode` accepted under the real provider;
  - the demo sign-in reachable without the flag, or from a non-local address;
  - the demo model answering an unknown document;
  - a settings file with an unknown key accepted;
  - a tenant's settings changed after startup.
- Every check above, `make test-integration`, `make ui-check`, every benchmark and `bench demo`.

Suggested commit: `docs: record verified M29 results`

## Approval and progress record

- Phase 2: design D1–D8 approved by the owner on 2026-11-26, each with the recommended option,
  the demo in the public repo.
- Phase 3: this plan, approved by the owner on 2026-11-26, including the S2 migration (after
  asking what its side effects are: existing runs read as no mode, the limited role's table grant
  covers the column, and a Postgres database must be migrated before the new API starts).
- S1: built on `packaged-demo`, reviewed and committed by the owner.
  - `gawe.core.tenant_settings`: `TenantSettings` (a schema that allows no extra keys, the
    defaults, per-tenant values merged over them and checked, all frozen; `for_tenant` hands out
    a copy) and `from_env`, which reads `GAWE_TENANT_SETTINGS` when set. `AppBundle.tenant_settings`.
  - Settled here: each app's `bundle(...)` takes an optional `settings` and otherwise reads the
    environment, so `main.py` and every existing caller are unchanged.
  - Settled here: the file holds one app's tenants, `{"tenants": {...}}`. One API runs one app,
    and a file keyed by app would quietly ignore a misspelt app name.
  - Contract review: `review_levels_always` (default `["low"]`), read by `low-risk-needs-legal`;
    `large_deal_threshold` (default 100,000), used for the app's variables and M27's captured
    variables; the two domain lists per tenant. `TENANT_VARIABLES` stays as the defaults.
  - IT access and event replay: schemas and defaults for their settings; IT's grant history and
    both apps' variables read the run's tenant. The default `GRANT_HISTORY` stays for callers
    outside a bundle.
  - The signature rules' 100,000 stays a literal, as the design says.
  - Tests: 33 in `tests/unit/test_tenant_settings.py` (the core: defaults and named tenants,
    copies, five bad-settings cases, the defaults and schema checks, the file's shape, the
    environment; each app's defaults equal its old values and a bad file stops it; per company,
    the low rating, the large-deal line, the competitor's domain, an unlisted tenant, M27's
    captured variables, a missing setting denying, IT's grant count and replay's limits). The
    boundary oracle gains three choices of always-reviewed levels; the unreadable-input suite
    gains the new variable for both assessment tools.
  - `make check` 3657 passed, 100 skipped; `make test-integration` 155 passed; check-docs 0
    problems; scripted, integrity 23, undo 4, replay 9 and record changes 18 identical to M28.
- S2: built, reviewed and committed by the owner.
  - `RunRecord.model_mode` (`honest` or `fooled`), migration 0012 (`runs.model_mode`, nullable,
    checked), the Postgres store reading and writing it. `run.created` carries it only when set,
    so every other run's payload keeps its shape.
  - Settled here: a model request carries an optional `RequestContext` (the run's mode and the
    content keys of its objective and document). The orchestrator fills it in. `request_hash`
    already listed its fields by name, so the context is not in any audit hash, and it is never
    sent to a model.
  - Settled here: the demo model picks a scene by the run's objective and checks the document
    is that scene's, because the orchestrator drops the document once read, and C-1003 appears
    in two scenes, one fooling the Reader and one the Planner. Content keys ignore line endings
    and surrounding space, so a pasted or uploaded sample matches.
  - `gawe.demo.scenes`: six scenes on the three sample contracts, each with an honest script;
    the two C-1003 scenes also have a fooled one (the Reader rates it low; the Planner emails
    the contract to `attacker@evil.example`). `gawe.demo.model.DemoModel` answers only these;
    anything else fails the run at reading, and nothing is guessed. The run records the model
    as `gawe-demo-scripted (honest)` or `(fooled)`.
  - `GAWE_LLM_PROVIDER` (`anthropic` by default, or `demo`); a run's `model_mode` is accepted
    only under a provider that says it takes one, and shown in the run and approval views.
  - On the default company, both C-1001 email scenes first wait on the low rating, so their
    email is judged after legal approves it. S3's expected records will show that.
  - Tests: 29 in `tests/unit/test_demo_model.py` (each scene's honest script, the fooled scripts,
    no mode meaning honest, five refusals, the context not in what a model is sent, content
    keys, six scenes' outcomes on the default company, honest and fooled side by side, the mode
    on the run, the card and the audit, the mode refused under another model and a bad mode, an
    unknown document and objective failing, the provider at startup); 2 in
    `tests/api/test_http_demo.py`; a Postgres round-trip in `tests/contract/test_run_store.py`;
    `tests/sql/test_run_model_mode.sql` (4 checks, including the app role).
  - A mistake, reported to the owner: the first throwaway-database Alembic check ran against
    the owner's development database instead (a zsh quoting slip), leaving it at 0012 with an
    empty `model_mode` column; no data was lost. The check was rerun guarded, on a real
    throwaway database.
  - `make check` 3689 passed, 101 skipped; `make test-integration` 157 passed; `make
    test-schema` 122 checks; check-docs 0 problems; Alembic upgrade, downgrade and upgrade on a
    throwaway database; scripted, integrity 23, undo 4, replay 9 and record changes 18
    identical to M28.
- S3: built, reviewed and committed by the owner.
  - `gawe.demo.companies`: Careful, Big-Deals and Rivals, each with fixed tenant ids and three
    people (Alice the case manager, Bob the legal reviewer, Fiona the finance approver), and
    `write_tokens`, which writes the API's token file (hashes only) and the UI server's sign-in
    file (person to token), both mode 0600, with fresh tokens each time.
  - `gawe/demo/settings.json`, the companies' contract review settings as D4 sets them, read
    through the same `TenantSettings` the API uses.
  - `gawe.demo.fixtures`: the tamper fixture, built in scratch stores and scratch mock systems
    as an ordinary completed run at Careful, rating C-1004 high. The copy of event 3 has its
    actor changed to "mallory" before the events are stored once each, so the chain links and
    verification reports it broken at event 3. The API installs it at startup only under the
    demo model on memory stores. Its objective ends "(Pre-built tamper fixture.)".
  - Settled here: the fixture rates C-1004, which no scene uses, in its own scratch systems, so
    its rating can't enter a company's rating history (M24) or the demo's contract system.
  - Settled here: the person ids are proper UUIDs, so the Postgres variant can store them.
  - `python -m bench demo` (`bench/demo.py`): 24 cases (every scene at every company, honest and,
    where a scene has one, fooled). When a step waits, the company's approver for that role
    approves it, until the run ends. Each record holds each step's outcome and approver role,
    how the run ended and the effects in the mock systems, against expected records written by
    hand from the companies' settings. All 24 matched on the first run.
  - Moved to S5, with the command that writes them: the test that the demo's token files are
    never written outside demo mode. In S3 nothing writes them on its own.
  - Tests: 9 in `tests/unit/test_demo_companies.py` (the settings file and each company's values,
    the people, the tokens and their files, fresh tokens, the fixture broken at event 3 and
    only there, its contract unused by any scene, the API in demo mode starting with it, and
    without the demo model none); 42 in `tests/bench/test_demo.py` (every case's record, every
    scene at every company, D4 row by row, the command's records, and a changed outcome
    reported).
  - `make check` 3740 passed, 101 skipped; `make test-integration` 157 passed; check-docs 0
    problems; scripted, integrity 23, undo 4, replay 9 and record changes 18 identical to M28;
    demo 24 of 24.
- S4: built, reviewed and committed by the owner.
  - API: `GET /v1/demo` (`gawe.demo.info`): the companies, their people (names, titles, roles),
    the scenes and the tamper fixture's run, with no token. Always registered, so M23's pinned
    route list holds it, and not found unless the API was started with the demo model.
    Settled here: it needs no sign-in, because the sign-in page lists the people from it.
  - Settled here: each scene carries a short "what to expect" text, shown on the Demo page.
    It lives in one place, `gawe.demo.scenes.EXPECT`, next to the scripts that `bench demo`
    checks.
  - UI:
    - `ModelModeLabel` ("Scripted demo model: honest" or "…: fooled") on the run page, the
      approval card and the audit page; the audit page now also loads the run for it;
    - `/demo/people`, one-click sign-in as any of the nine people;
    - `/demo`, the guided page: the companies and what their settings do, each scene with what
      to expect, "Start (honest model)" and "Start (fooled model)" per company, "Decide as Bob
      (legal)" and "… Fiona (finance)", the Big-Deals note, and the tamper fixture;
    - a link to both from the login page in demo mode.
  - The gate (`lib/demo-view.ts`, `demoEnabled`): the UI started with `GAWE_UI_DEMO=1`, the API
    answering `/v1/demo`, and a request to localhost, `127.0.0.1` or `[::1]` by its Host header.
    Otherwise both pages are not found, the login page shows no link, and the demo's actions do
    nothing. Each action checks the gate again. Tokens are read on the UI's server from
    `GAWE_UI_DEMO_SIGNIN` and never sent to the browser except as the usual httpOnly cookie.
  - Limit: a Host header is what the client says. The localhost check stops casual exposure,
    not a client that can already reach the UI; S5's `make demo` binds both servers to
    127.0.0.1 for that reason.
  - Tests: UI, 5 model tests in `tests/demo-view.test.ts` (the label, localhost names including
    look-alikes, the gate's three conditions, people by key and role, the sign-in file) and 5
    rendered tests in `tests/demo.render.test.tsx` (the label; a scripted run's approval card;
    the sign-in page's people with no token on the page; the Demo page's scenes, fooled only
    where a scene has it, the decide buttons, the fixture and the Big-Deals note; both pages
    not found without the gate). Python, 2 in `tests/api/test_http_demo.py` (`/v1/demo` not
    found outside demo mode; its people and scenes with no token), the startup test checking
    `/v1/demo` names the fixture, and `/v1/demo` in M23's route list.
  - `make check` 3742 passed, 101 skipped; `make ui-check` 77 model and 26 rendered tests and
    the build; `make test-integration` 157 passed; check-docs 0 problems; scripted, integrity
    23, undo 4, replay 9 and record changes 18 identical to M28; demo 24 of 24.
- S5: built, reviewed and committed by the owner; merged to `main` on 2026-11-27.
  - `make demo` runs `python -m gawe.demo run`:
    - it checks for Node and npm, the UI's packages and free ports 8000 and 3000, and starts
      nothing if one is missing;
    - it writes fresh tokens for the nine people into `.demo-data/demo/tokens/` (ignored, 0600);
    - it starts the API on 127.0.0.1:8000 (memory stores, `GAWE_LLM_PROVIDER=demo`, the
      companies' settings, a scratch mock-systems folder, signing and review-sample keys made
      for the run unless `.env` sets them);
    - it waits for `/healthz`, starts `next dev` on 127.0.0.1:3000 with `GAWE_UI_DEMO=1`, prints
      http://127.0.0.1:3000/demo and opens nothing;
    - on Ctrl-C it stops both, deletes the scratch folder and puts back `ui/next-env.d.ts`,
      which `next dev` rewrites, so a demo leaves git clean.
  - Settled here: `next.config.mjs` sets `agentRules: false`; Next 16.4 otherwise writes
    `ui/AGENTS.md` on every `next dev` start.
  - `make demo DEMO_STORE=postgres` runs `scripts/demo_postgres.sh` after `make up`. Settled
    here: it uses its own database, `gawe_demo`, never the development one named in `.env`. It
    creates it if missing, checks that the migration target is `gawe_demo` before running
    Alembic, and seeds the companies' tenants and users (`python -m gawe.demo seed-sql`) behind
    a SQL guard on `current_database()`. Runs persist; tokens are new each start. `make
    demo-reset` drops `gawe_demo` and `.demo-data/demo`. The real tamper scene is documented
    against `gawe_demo`.
  - The old `make demo` (Postgres and the real model in one go) is replaced; the step-by-step
    real-model setup in `docs/demo-script.md` is unchanged.
  - Tried by hand in the browser:
    - memory stores: the Demo page, the fooled C-1003 review at Big-Deals going through, the
      tamper fixture reporting `Chain BROKEN at event 3`, and Ctrl-C stopping both servers;
    - Postgres: the honest C-1001 review at Careful waiting for legal, approved as Careful's
      Bob and completing. The run is in `gawe_demo`, and the development database has neither
      it nor the companies' tenants.
  - Docs: the README leads with "See it in the browser"; `docs/demo-script.md` starts with the
    packaged demo (what differs, the scenes table, the fixture, `bench demo`, the Postgres
    variant with the real tamper and reset); `docs/api.md` covers `GAWE_LLM_PROVIDER`,
    `model_mode`, `/v1/demo` and `GAWE_TENANT_SETTINGS`; threat model T48 (the demo sign-in's
    gating), T49 (scripted output passed off as real) and T50 (settings loosened at run time);
    the roadmap's M29 row.
  - Tests: 15 in `tests/unit/test_demo_launcher.py`, covering:
    - what each server is given, including overrides of `.env`'s provider and store;
    - loopback only;
    - problems stopping the run before anything starts, a busy port, the Postgres signing key;
    - the seed's companies and people, with no value needing quotes;
    - `prepare`, an unknown command, the token folder ignored by git;
    - the API writing no token files in demo mode;
    - the demo API starting without the `anthropic` package.
  - Negative matrix on copies of the tree, after a passing control (80 passed, 1 skipped: the
    git check, since a copy has no `.git`; UI 5 and 5). Each mutation fails its test:
    - `model_mode` accepted under the real provider: 2 tests;
    - the demo sign-in without the flag, from a non-local address, or without the API's demo
      info: 1 model test each;
    - the demo model answering an unknown document;
    - tenant settings not checked (an unknown key accepted): 4 tests;
    - a schema that allows extra keys;
    - `for_tenant` handing out the stored, unfrozen settings;
    - the API writing token files in demo mode;
    - `/v1/demo` answering outside demo mode.

    Log: `bench-results/m29-s5-negative.txt`.
  - `make check` 3757 passed, 101 skipped; `make ui-check` 77 model and 26 rendered tests and
    the build; `make test-integration` 157 passed; check-docs 303 references, 0 problems;
    scripted 38 identical to the 2026-11-26 baseline; integrity 23, undo 4, replay 9 and record
    changes 18 match their records; demo 24 of 24. Log: `bench-results/m29-s5-checks.txt`.
  - Limits: the scripted model shows what policy does, not how often a real model is fooled;
    the Host check trusts the header (both servers bind to 127.0.0.1); memory mode's tamper
    scene is a fixture; contract review only.
