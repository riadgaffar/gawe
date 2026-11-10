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
| M13  | Testing the second line of defence: a free scripted benchmark run that sends every attack through the engine with a model fooled on purpose, two rules for "low" CRM ratings, the clean-run policy table, and the decision on internal email and routine access grants | Done (2026-11-10: of 38 scripted runs, 10 attacker steps ran, all allowed by policy on purpose; 5 denied; 23 waited for a person. A "low" rating now needs legal approval when the Reader's findings contradict it or the deal is over 100,000. See [m13-design.md](m13-design.md) and [results.md](results.md#when-the-model-is-fooled)) |

Planned, not scheduled: **Pilot-ready** (OIDC sign-in and the first real connector against a sandbox
account). It starts when there is a design partner or a sandbox to connect to, and gets a number then.

M5.5 was not in the original plan; the orchestrator needs it before anything runs against a real
database. UI and the IT-access fixture are the first things to cut if scope must shrink.

## After M13: candidates, not scheduled

- **Why the Reader over-flags `unlimited_liability`.** In the first run its precision on CUAD was
  33% (61 of 91 flags on contracts CUAD does not mark), against 69–74% for the other scored
  kinds. Its definition of "uncapped" may be wider than CUAD's; compare the flagged contracts.
  A yes/no field may be the cause: try "capped / capped with carve-outs / uncapped" instead.
  Since M13 this also sets how often legal is asked to approve an honest "low" rating: the rule
  for contradicted ratings fires on any high-severity flag, so over-flagging becomes needless
  approvals, not just a wrong number.
- **Approval by what an action changes, not by deal size.** A reader's principle: any action that
  changes what anyone can do later (access, risk ratings, approval thresholds, the policy itself)
  always goes to a person, however small, because lowering the bar is usually an attacker's first
  step. Thresholds and policy are already out of reach (no tool changes them). Taken literally, the
  rest means a person on every review and every access request: risk writes and grants happened
  in 15 of 15 clean runs each. One option to weigh against it: classify each tool action by
  whether it lowers the bar (a lower risk rating, wider access) and require approval for those
  only, perhaps per tenant, replacing the 100,000 deal-size line with a principle. Decide with
  the approval volume from the next live run in hand.
- **Policy never reads state the agent can write.** Today every rule judges a step on its own
  arguments plus inputs no earlier action can change: fixed tenant settings, the deal value in the
  contract system (no tool writes it), and the Reader's findings for that run only. So a series of
  allowed actions cannot drift a workflow into a state where a later gate is weaker (a reader's
  concern: three harmless writes in a row making a destructive one reachable). That holds only
  for the current rules; one like "low-risk contracts skip signature approval" would break it.
  Make it explicit: a check that no policy variable comes from data any registered tool writes,
  plus sequence fuzzing as a tripwire (run random sequences of allowed actions, then check that
  every attacker goal still ends as expected). The fuzzing passes trivially today and fails the
  day a rule starts reading writable state.
  Exception worth having: rules on combinations, which is where an attacker's sequence shows
  (a second downgrade on a contract that has already been lowered; a downgrade plus a grant on
  the same deal, once workflows share a deal). Allow history only to escalate, never to relax:
  a rule may read it to add an approval, never to remove one, so the worst an attacker gets is
  more reviews. Read that history from the audit trail, which the engine writes and no tool can,
  not from records the agent's own tools wrote. (Suggested by a reader.)
- **Automatic grants expire.** Access granted without a person's approval should carry an end
  date, tied to the request (for example, when the ticket or review closes), so the risk accepted
  by allowing routine grants is time-boxed. `iam.grant_access` has no expiry today; approved
  grants could last longer. (Suggested by a reader.)
- **Revisit allowing internal email without approval.** It was allowed on fatigue grounds, but
  none of the 30 clean runs sent any email, so requiring approval would have added no reviews in
  the measured workflows, while the 3 internal-email attacks ran. Real workflows that send internal
  notifications may differ; check before changing. (Raised in reply to a reader.)
- **Grants only to the requester the Reader reported.** When only the Planner is fooled, the
  attacker's grant goes to someone other than the ticket's requester (3 of the 6 grant attacks
  that ran). A rule comparing the grant's email with the Reader's `requester_email` would stop
  those; it cannot stop a fooled Reader that reports the attacker's choice as the requester, the
  same limit as the contradiction rule. Same-tool attacks like these are also why a daily cap
  would not single them out, while a next-day review could.
- **A deterministic scan for liability language, as a downgrade signal.** The contradiction rule
  only fires when the Reader flags a clause, so it is bounded by the Reader's recall; a fully
  fooled "low" with nothing flagged on a deal of 100,000 or less still writes without a person.
  Someone planting instructions in a contract can add text but usually cannot remove the real
  liability or indemnity clause, so a scan of the document for that language would still find it,
  whatever the Reader reports. Use it as one more signal for the "low" rules, not as a judge:
  paraphrase defeats keywords, and a counterparty who wrote the whole contract controls the
  wording. (Suggested by a reader.)
- **Check state, not just the tool's reply.** Post-conditions today compare a tool's own reply
  with what was asked ("the assessment I wrote says low for C-1002"). For writes, read the record
  back from the system with a separate call and check that instead. Run status already never
  comes from a model's prose; the executor decides it. (Raised by a reader.)
- **Injection through tool outputs.** No model reads tool outputs today: the Planner plans once
  from the Reader's findings, and later steps receive earlier outputs as arguments (`$ref`),
  which policy checks on their final values. Two gaps: no attack yet puts poisoned data in a
  connector's output (say, a contract record's contact address) to see it become an argument;
  and automatic replanning, deferred since M7, must route tool outputs through the quarantined
  Reader, never to the Planner, if it is ever added. (Raised by a reader.)
- **How often policy steps in on honest work.** "30 of 30 clean runs finished" measures utility,
  not how often policy denied, held for approval, or changed an action a person would have
  allowed. Report interventions on clean runs, with an interval, in every live report. The 30
  runs are 6 documents repeated 5 times, so the honest unit is the document and 0 of 6 still
  allows a high rate; add more clean documents rather than more repeats. The first run had no
  interventions on clean runs, but that predates the M13 rules. (Raised by a reader.)
- **Fuzz arguments against the rules.** An attacker who knows the rule file aims at its broad
  allows, which is where all 10 scripted attacker steps that ran landed. Generate argument
  variants aimed at the rules themselves (lookalike domains, subdomains, case and encoding
  tricks, recipient lists and display names) and check that every near-miss ends in approval or
  deny, never allow. Some edge cases are tested already (a comma-joined recipient list is not
  judged by its first address); a domain that is not exactly internal should fall into the
  outside-email rule, but that is reasoned, not fuzzed. (Raised by a reader.)
- **Injection through short text fields.** The benchmark checks that attacker text never reaches
  a stored record, but the mock systems only store strings. A mock that interprets its inputs
  (as formatting, templates or commands) would test whether a bounded field the Reader passes on
  can still carry a payload into a downstream system.
- **Approve once for a kind of action.** A person approves a kind of message once; matching sends
  then run unattended under a daily cap, and one bad result or one edit by the owner puts that
  kind back to manual. Today every approval covers exactly one action.
- **A next-day review job, with a sampled review of clean downgrades.** Re-check the previous
  day's automatic actions against the written rule, using the audit trail. Include a sample,
  about 5%, of "low" ratings that went through with nothing flagged, for a person to check: the
  one case the downgrade rules let through, and the only way to measure how often the Reader
  misses a real risk. Choose the sample from the action's hash rather than at random, so it is
  reproducible and auditable like the rest of the policy. (Suggested by a reader.) Size the rate
  from the claim it has to support: zero misses in about 32 samples only shows a miss rate under
  roughly 9%; under 1% needs about 300. Decide the hash key deliberately: a stable key (contract,
  clause, model version) keeps the sample from reshuffling when a record changes, but then the
  same contracts stay in or out until the model changes.
- **More distinct attacks, not more repeats.** The 125 injection runs are 25 attacks repeated 5
  times, so the honest sample is closer to 25 (a 95% upper bound of about 13%). Bringing that down
  needs a few hundred attacks that genuinely differ. Model spend is small (about $0.015 a run);
  writing them is the cost. If they are generated, check that they test something different
  rather than varying 25 templates.
- **A bigger contract sample, if paired comparison is not enough.** Treated as independent
  samples, two setups 5 points apart need about 330 items each to tell apart (12% of answers
  change between repeats); the current sample has 153 (51 contracts, 3 clause types), enough for
  about 7 points. But comparing two setups on the same contracts is paired: differences between
  contracts cancel and the remaining noise is the 12% already measured, so 153 items compared per
  (contract, clause type), with the interval from resampling whole contracts as the existing
  intervals already do, may be enough. Grow the sample only if that interval is too wide. CUAD
  has 495 usable contracts, at about $0.04 per read.
- **A Haiku 5.5 comparison run** on the same attacks and contracts, to see whether a cheaper model
  is good enough for the Reader or Planner. Compare the paired difference, not each model's
  interval: a permutation test over contracts (swap a whole contract's answers between the two
  models at random, about 10,000 samples, and recompute the metric, which works for ratios like
  precision). Report first how many contracts the two models disagree on: with k of them, the
  smallest possible p-value is about 2^-k (five contracts cannot go below about 0.03 one-sided),
  however many samples run. Size the run by the disagreement expected. In practice noise, not
  that floor, is the limit: with 12% of answers changing between repeats, about a third of
  contracts (around 16 of 51) would show a disagreement even between identical setups. Noise does
  not invalidate the test, but it costs power, so compare each model's majority answer over its 3
  reads and report separately the disagreements that hold across repeats. (Agreed with a reader
  in the comments on the published write-up.)

## Decisions in force

- A "low" CRM risk rating needs legal approval when the Reader's findings flag a high-severity
  clause or the deal in the contract system is over 100,000 (M13). A fooled "low" with nothing
  flagged on a smaller deal is allowed, on purpose.
- Internal email and non-admin, non-production access grants to company accounts run without
  approval (decided in M13): approval on every one would be clicked through. Revisit with real
  traffic. Internal email is under review (see "After M13"): no clean run sent any email.
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
