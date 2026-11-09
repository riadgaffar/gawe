# GAWE roadmap

Each milestone ends green: `make check` (ruff, mypy, pytest) plus any integration or schema tests it
adds. If verification fails twice on a milestone, stop and report instead of looping.

| #    | Milestone | Status |
|------|-----------|--------|
| M0   | Scaffold: tooling, Compose with Postgres, README | Done |
| M1   | Contracts and primitives: models, canonical hashing, registry, plan validator | Done |
| M2   | Policy engine: rules, default-deny, HMAC-signed decisions | Done |
| M3   | Persistence and audit: schema, hash-chained append-only audit log | Done (integration tests pass) |
| M4   | State machine and approvals (hash-bound, expiring, separation of duties) | Done |
| M5   | Executor and verifier (idempotency, retries, lease-based resume) | Done |
| M5.5 | Postgres stores for steps, approvals and executions, migration 0002, shared contract tests | Done (47 integration tests pass on Postgres 16) |
| M6   | LLM layer: provider interface, fake provider, quarantined Reader, structured-input Planner | Done (Anthropic provider verified against the live API with `make smoke`) |
| M7   | Orchestrator (`gawe/engine`) and OpenTelemetry: run loop, metrics. Automatic replan deferred | Done; the OTel adapter is untested against a real SDK |
| M8   | FastAPI API, event stream, bearer-token auth stub with roles, Postgres run/plan store (migration 0003) | Done (HTTP and Postgres run-store tests pass on a development machine) |
| M9   | Contract Review app, mock connectors (dedupe on idempotency key), IT-access fixture, synthetic samples, live smoke script | Done (real-model smoke and full demo verified) |
| M10  | Next.js (TypeScript) UI: approval inbox, run timeline, audit and chain-verify view | Done (`make ui-check` passes; demo verified end to end) |
| M11  | Hardening and docs: threat model, architecture, API-boundary adversarial tests, least-privilege DB role, failed-sign-in limit, closing approvals when a run ends, restart recovery | Done (`make check` passes; role migration verified on Postgres 16; `make test-schema` passes, 71 checks; `gawe_app` confirmed able to log in on the dev database, 2026-11-08) |
| M12  | Evidence: reproducible benchmark harness (CUAD Reader accuracy; injection test counting model-fooled and unauthorized-step-ran separately, with run-to-run variance), external audit-head anchor, and a plain-language report | Done (first full run on `claude-sonnet-5-5`, 2026-11-08: 0 of 122 attack trials fooled the model, 0 attacker steps ran, 30 of 30 clean twins completed; clause recall 94%, precision 51%; $15.77 including the trial. See [m12-design.md](m12-design.md)) |

Planned, not scheduled: **Pilot-ready** (OIDC sign-in and the first real connector against a sandbox
account). It starts when there is a design partner or a sandbox to connect to, and gets a number then.

M5.5 was not in the original plan; the orchestrator needs it before anything runs against a real
database. UI and the IT-access fixture are the first things to cut if scope must shrink.

## After M12: candidates, not scheduled

- **Why the Reader over-flags `unlimited_liability`.** In the first run its precision on CUAD was
  33% (61 of 91 flags on contracts CUAD does not mark), against 69–74% for the other scored
  kinds. Its definition of "uncapped" may be wider than CUAD's; compare the flagged contracts.
  A yes/no field may be the cause: try "capped / capped with carve-outs / uncapped" instead.
- **Whether internal email, low-risk CRM records and low-level access grants should need
  approval.** Policy allows all three without a person, so a fooled model could cause them. The
  first run never fooled the model, so this stayed theoretical; it is a policy decision, not an
  engine change.
- **Record which layer stopped each attack.** The injection report counts a fooled model and an
  attacker step that ran, but not whether the Reader never emitted the value, plan validation
  rejected it, policy denied it, or it waited for an approval. With a model that gets fooled,
  that tells the defences apart.
- **Injection through short text fields.** The benchmark checks that attacker text never reaches
  a stored record, but the mock systems only store strings. A mock that interprets its inputs
  (as formatting, templates or commands) would test whether a bounded field the Reader passes on
  can still carry a payload into a downstream system.
- **A Haiku 5.5 comparison run** (about $0.80) on the same sample and attacks, to see whether a
  cheaper model is good enough for the Reader or Planner.
- **Publishing a report in `docs/`** once one has been read and accepted (design decision 8).

## Decisions in force

- Python 3.12+ backend (FastAPI), Postgres, own state machine now (Temporal later), TypeScript UI.
- The policy engine, not the LLM, authorizes execution. Plans are untrusted input.
- Approvals bind to the action hash (tool, version, resolved args, run, step).
- Dual-LLM design: a quarantined Reader with no tools; a Planner that sees structured findings only.
- Secrets come from the environment only.

## Known limits and open items

- Audit tail truncation and rewritten history are detected against an anchor kept outside the database (`GAWE_ANCHOR_SINKS`, off by default): a local file, and/or a signed RFC 3161 timestamp (`rfc3161`, needs the `anchor` extra). Each finished run is one request to the timestamp authority; batching many heads into one timestamp comes later if volume needs it.
- A model that refuses or runs out of output tokens is a failed Reader or Planner attempt; after the last attempt the run fails (`reading_failed` or `planning_failed`) instead of stalling in `planning`.
- Benchmark results live in `bench-results/` (not committed) and are reproducible from the manifest in each run directory. Runs on other models, prompts or dates need a new run.
- Run `make app-role` and set `GAWE_APP_DB_PASSWORD` so the API uses the limited role; without it the API falls back to the owner and logs a warning.
- "Exactly once" external effects require connectors to deduplicate on the idempotency key; the
  connector contract is implemented by `gawe.connectors.base.Ledger` and tested; real connectors must do the same.
- Policy decisions are stored in `runs.decisions` (jsonb); the `policy_decisions` table from 0001 is unused for now.
- Interrupted runs are driven again at startup (`GAWE_AUTO_RESUME=0` disables); `POST /v1/runs/{id}/resume` does it on request.
- The event stream polls the audit log once a second; fine for an MVP, not for thousands of viewers.
- The approval inbox scans all open approvals and filters in Python; add a tenant-scoped query before scale.
- API tokens are a stub (hashed tokens in a file); replace with OIDC and database roles.
- Only one in-process driver per run (a lock); a multi-worker deployment needs a database lease.
- Steps run one at a time in plan order; no parallel execution.
- Replanning after a failed step is designed (run `running -> planning`) but not implemented.
- Open approvals are closed (rejected by the system) when a run ends; the inbox also filters by step status.
- Policy sees the arguments the model proposed. Where an argument carries a fact policy relies on (the deal value), the tool re-checks it against the system of record and refuses on a mismatch.
- Uploads accept text and Markdown only; PDF/DOCX extraction is a separate parsing risk and comes later.
- Failed-sign-in limiting is per address; behind the UI all users share one address. See docs/threat-model.md.
- Dependencies are not scanned automatically; `npm audit` reported no issues in the UI on a fresh install (2026-11-09).
- Reader free-text fields are still attacker-influenced. Keep them to a minimum in each app's
  Reader schema and prefer enums, numbers and booleans.

## Per-milestone testing

- Unit tests for every new module; adversarial tests for anything on the authorization path.
- Every new state transition needs a test (`tests/unit/test_statemachine.py`).
- Policy changes need golden-file cases (`tests/fixtures/example_policy_cases.yaml`).
