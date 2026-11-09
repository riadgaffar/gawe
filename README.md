# GAWE: Governed Agentic Workflow Engine

An AI agent receives a business objective, discovers tools, drafts a plan, requests human
approval for sensitive actions, executes only what policy authorizes, and leaves a tamper-evident
audit record.

**Core principle: the policy engine, not the LLM, authorizes execution.** The LLM proposes;
deterministic code disposes.

## Layers

| Layer | What | Status |
|---|---|---|
| 1. Core | Domain-agnostic orchestration engine: policy, approvals, executor, audit | Done |
| 2. Integration platform | REST API (`docs/api.md`) and Next.js UI (`ui/`) | Done |
| 3. Business apps | Contract Review and IT access reference apps | Done |

Design: [docs/architecture.md](docs/architecture.md). Attacks and their tests:
[docs/threat-model.md](docs/threat-model.md).

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

## Benchmark (M12)

`bench/` measures the Reader's accuracy on CUAD (public contracts labelled by lawyers) and runs a
prompt-injection suite through the full engine, counting "model fooled" separately from
"unauthorized step ran". Design: [docs/m12-design.md](docs/m12-design.md). It needs the `llm`
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
src/gawe/apps/           Contract Review and IT-access applications (tools, policies, samples)
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
