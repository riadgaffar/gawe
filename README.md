# GAWE: Governed Agentic Workflow Engine

An engine for AI agents that act on business systems (sending email, recording risk ratings,
requesting signatures, granting access) where the documents they read can't be trusted.

**The model proposes, deterministic code decides.** A quarantined Reader turns an untrusted
document into structured findings; a Planner that never sees the document proposes a plan; a
default-deny YAML policy engine decides each step (allow, needs approval, deny) and signs the
decision; approvals bind to a hash of the exact action; and every event lands in a hash-chained
audit log whose head can be anchored outside the database.

It's measured, not just claimed ([docs/results.md](docs/results.md)):

- **Live:** 64 distinct prompt-injection attacks against Claude Sonnet 5.5 fooled it zero times
  (95% upper bound 5.7%).
- **With the model fooled on purpose:** every attack scripted to succeed against the model, then
  run through the real engine. 3 of 38 attacker steps still go through, all internal emails, which
  the policy allows by a recorded decision; 8 are denied and 27 wait for a person.
- **What it doesn't show** is on the same page: the attacks are written in advance, the systems
  are mocks, and one model family was tested.

Why it was built this way, and what readers' critiques changed:
[the write-up on dev.to](https://dev.to/riadgaffar/i-ran-125-prompt-injection-attacks-on-my-ai-agent-none-got-through-and-that-wasnt-the-4ib6).

## Try it in a minute

Needs Python 3.12 or newer. No API key, no Docker, no spend:

```bash
python -m venv .venv && source .venv/bin/activate
make install                 # core + db + dev dependencies
make check                   # ruff + mypy --strict + the full test suite, on a fake model
python -m bench scripted     # every attack, model fooled on purpose; writes a report
python -m bench record-changes  # 18 record-change evidence cases, no model calls
```

## What it enforces

- **Plans are untrusted.** Structure, tools, dependencies and argument types are validated before
  anything is considered for execution.
- **Policy decides every side effect**, default deny, strictest outcome wins, signed with a key
  the model never holds. Policy refuses to start if a rule doesn't match a tool's schema, or if an
  allow rule could match a value it can't read.
- **Approvals bind to the exact action**, including the version of every record it refers to;
  any change, or a record replaced underneath, invalidates the approval. The approval screen shows
  typed fields first and collapses text the model wrote.
- **Refused record changes are explained:** approval cards show captured policy fields; refusals
  compare saved observations and distinguish atomic-guard evidence from later reads. Matching
  fields never cancel a revision refusal. Missing history stays explicitly unavailable.
- **Writes are checked against the target, not the tool's reply**: read back from the system's
  own record, with pending states, deadlines and a scheduled sweep. Email goes only to the
  mailboxes a directory expands, and each connector declares what its system actually guarantees.
- **Some actions are only prepared:** an action class can be configured so GAWE drafts it and a
  person completes it, with nothing able to dispatch it.
- **Access is bounded:** a grant to anyone other than whoever filed the ticket, or from a ticket
  that came in by email or a public form, waits for a security approver; automatic grants expire.
- **The audit log is tamper-evident:** each event hashes its predecessor. Anchoring the chain's
  head outside the database is opt-in (`GAWE_ANCHOR_SINKS`): a local file sink needs storage the
  database owner can't modify, and an RFC 3161 timestamp proves the head existed at a time, not
  that the events before it were correct.

Design: [docs/architecture.md](docs/architecture.md). Every attack and the test that covers it:
[docs/threat-model.md](docs/threat-model.md). Milestones and what's next:
[docs/roadmap.md](docs/roadmap.md).

## Quick start

Needs Python 3.12 or newer. `make check` needs nothing else; the steps after it also need
Docker (Postgres runs in a container on port 5432, so stop any local Postgres first or change the
port in `docker-compose.yml`) and, for the UI, Node 22.6 or newer.

```bash
python -m venv .venv && source .venv/bin/activate
make install      # core + db + dev dependencies
make check        # ruff + mypy --strict + pytest
```

Postgres, API and UI (see [docs/demo-script.md](docs/demo-script.md) for the full walkthrough):

```bash
make up                # creates .env with generated secrets on first run, starts Postgres
make migrate           # applies migrations/sql/*.sql through Alembic
make test-schema       # SQL-level tests of constraints and the append-only audit triggers
make app-role          # creates the limited database role the API runs as (see below)
make test-integration  # Python tests against the live database (isolated schema)
```

Running the API, the demo or the benchmark calls a real model. Install its SDK with
`pip install -e ".[llm]"` and set `ANTHROPIC_API_KEY` and `GAWE_LLM_MODEL` in `.env`; calls are
billed to that Anthropic account. `make check` uses a fake model and needs neither.

See [docs/roadmap.md](docs/roadmap.md) for milestones and status.

Each milestone was built in steps in a private development repository and lands here as one
commit. Commit ids in the design, plan and results documents refer to that private history: in
the results they name the exact code each benchmark run measured.

## Benchmark (M12)

`bench/` measures the Reader's accuracy on CUAD (public contracts labelled by lawyers) and runs a
prompt-injection suite through the full engine, counting "model fooled" separately from
"unauthorized step ran". Design: [docs/m12-design.md](docs/m12-design.md); results of the first
run: [docs/results.md](docs/results.md). It needs the `llm`
extra and `ANTHROPIC_API_KEY` and `GAWE_LLM_MODEL` in `.env`; the model must be in
`bench/prices.yaml`.

```bash
make bench-data                       # download CUAD (~106 MB) into .bench-data/, check its hash
make bench-estimate MAX_USD=1         # free: exact input cost and the worst case
python -m bench estimate --max-usd 2 --trial   # paid (up to $2): an expected total
make bench MAX_USD=25                 # the run; refuses if it may not fit, asks before starting
python -m bench report bench-results/<run id>  # rebuild report.md from a run's files
python -m bench run --max-usd 15 --resume bench-results/<run id>  # continue a stopped run
```

Every command that can spend needs a cap: there is no default. A run stopped by its cap or by the
API (for example, no credit left) keeps every finished trial; `--resume` continues it without
paying for those again, and its `--max-usd` caps only the new spend. Results go to `bench-results/`
(ignored by git); nothing there holds document text or model output. `make check` never calls a
model: the bench tests use fixtures and a fake model.

## Layout

```
src/gawe/core/           contracts, canonical hashing, registry, plan validator
src/gawe/core/policy/    rule loading and matching, RuleEngine, signed decisions
src/gawe/core/audit.py   per-run hash-chained audit log, verify_chain, in-memory store
src/gawe/core/statemachine.py  the only legal run/step status transitions
src/gawe/core/approvals.py     hash-bound, expiring, separation-of-duties approvals
src/gawe/core/executor.py      the only code path that runs a tool (idempotent, retried, resumable)
src/gawe/core/verifier.py      output-schema and post-condition checks before a step succeeds
src/gawe/core/records.py       step/execution records and store interfaces (in-memory reference)
src/gawe/llm/            provider interface, quarantined Reader, privileged Planner, fake provider
src/gawe/core/runs.py          run records and store interface (in-memory reference)
src/gawe/core/refs.py          resolves `$ref` arguments from earlier steps' saved outputs
src/gawe/core/telemetry.py     spans and metrics interface; no-op, in-memory and OpenTelemetry
src/gawe/engine/         orchestrator (the run loop) and startup recovery of interrupted runs
src/gawe/maintenance/    scheduled verification sweep, heartbeats, `python -m gawe.maintenance`
src/gawe/apps/           Contract Review, IT access and event replay (tools, policies, samples)
src/gawe/connectors/     mock systems with idempotent, ledger-backed effects
src/gawe/anchoring/      audit-head anchor sinks: local file, RFC 3161 signed timestamps
bench/                   M12 benchmark harness (CUAD accuracy, injection suite, report)
ui/                      Next.js client (approval inbox, run timeline, audit verify)
src/gawe/api/            token auth, framework-free service, FastAPI routes (docs/api.md), wiring
src/gawe/db/             SQLAlchemy session, Postgres stores (audit, steps, approvals,
                         executions), SQL script runner
migrations/sql/          the schema, as plain SQL (testable with psql)
migrations/versions/     Alembic revisions that wrap the SQL files
scripts/test_schema.sh   applies the SQL and runs tests/sql against a throwaway database
tests/                   unit, golden policy cases, adversarial, SQL schema tests,
                         tests/contract (same store tests on in-memory and Postgres)
```

## How enforcement works

1. **Plans are untrusted.** `validator.py` checks structure, tools, dependencies and argument
   types before anything is considered for execution.
2. **Every concrete action gets a signed decision.** `RuleEngine` applies RBAC, schema checks and
   declarative rules (deny > needs_approval > allow, default-deny) and signs the result with an
   HMAC key the LLM never holds. The executor will accept only a decision whose signature
   verifies and whose `action_hash` matches the action it is about to run.
3. **The executor re-checks everything.** It recomputes the action hash, verifies the decision's
   signature for that hash, requires an unexpired approval for that exact hash when policy asks
   for one, wins the step by compare-and-set, and passes one idempotency key to every attempt.
   Output must pass schema/post-condition verification before a step can succeed.
4. **Approvals bind to the exact action.** `action_hash` covers tool, version, resolved args, run
   and step; any change invalidates the approval.
5. **Documents never reach the Planner.** A quarantined `Reader` (no tools, fenced input, strict
   output schema) turns a document into `Findings`; the `Planner` accepts only a `Findings` object
   and sees it inside a marked data block. A hijacked model on either side gains no authority:
   the plan is validated, and every step is still judged by the policy engine.
6. **The audit log is tamper-evident.** Each event hashes its predecessor. Postgres triggers
   enforce linkage and block UPDATE/DELETE/TRUNCATE; `verify_chain` detects out-of-band edits.
   Tail truncation needs an externally anchored head (`expected_head`).

Database role: `make app-role` (migration 0004 plus `scripts/create_app_role.sh`) creates
`gawe_app`, which the API uses when `GAWE_APP_DB_PASSWORD` is set. It can insert and read audit
events but cannot update, delete or truncate them. An owner can still drop the triggers; the hash
chain exists to detect exactly that. Without the variable the API falls back to the owner and logs
a warning.

## Non-negotiables

- The LLM never executes anything and never holds credentials.
- Every side-effecting action passes the policy check; there is no bypass path.
- Approvals bind to the exact action (tool, version, resolved args) via a SHA-256 hash.
- Secrets come from the environment only.
- Document content is data, never instructions.

## Database notes

- Stores take a SQLAlchemy `Connection` (join the caller's transaction) or an `Engine` (each call
  commits by itself). The executor must use the Engine form so its claim on a step and its attempt
  records are durable before a tool runs.
- Steps are looked up by `(run_id, step_key)` in the run's *current* plan version, so a replan
  never inherits approvals, attempts or outputs from an older plan.
- Tool output is kept in `step_outputs` (write-once). Audit events hold hashes only.

## Licence

MIT; see [LICENSE](LICENSE).

Third-party material, credited and kept under its own terms:

- **CUAD** (The Atticus Project, CC BY 4.0) is used by the benchmark but not included: `make
  bench-data` downloads it and checks a pinned hash. The fixtures in `tests/bench/fixtures/cuad/`
  are synthetic contracts in CUAD's file layout.
- The timestamp-authority root certificates in `src/gawe/anchoring/tsa_certs/` (DigiCert,
  USERTrust/Sectigo) are public certificates, copied from the `certifi` bundle.
