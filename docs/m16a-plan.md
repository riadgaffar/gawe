# M16a build plan

Status: **done** (2026-11-15). S1–S5 complete and committed; S6 verification passed and was
committed by the owner.
The owner approved D1–D5 in [m16a-design.md](m16a-design.md). This plan implements those decisions.
The owner separately approved this plan before S1 implementation.

## Working rules and verification

Work only on `m16a-sending-readback`. Preserve existing owner changes. Do not commit; the owner
reviews and commits each checkpoint. Do not edit the roadmap, other repositories or unrelated
features. No live model calls, real mail/directory integration or automatic replanning. Do not
implement general record-version approvals, content filtering, grant expiry, reusable approvals,
approval-content redesign, unreadable-decision markers or next-day review.

Run steps S1–S6 in order. At each checkpoint provide the diff summary, actual verification output
(with a complete local log), known limitations and suggested one-line commit message, then stop
for owner review. If verification fails twice on the same step, stop and report rather than
continuing a repair loop. A substantive change to approved security semantics needs a new owner
decision; routine implementation details may be resolved within this plan.

At each implementation checkpoint run:

```sh
PATH="$PWD/.venv/bin:$PATH" make check
PATH="$PWD/.venv/bin:$PATH" make check-docs
```

For schema changes also run `make test-schema` and applicable Postgres store-contract/integration
checks through the project virtualenv. For UI changes run `make ui-check`. Use the existing local
test database workflow, never a production database. Report unavailable prerequisites as incomplete
verification; do not substitute skipped database tests for schema/concurrency evidence.

Use focused tests first. Retain outputs in ignored `bench-results/m16a-sN-*.txt` files. Do not add
threat-model references to tests that do not yet exist. Update the design build record with each
checkpoint's actual changes and results; neither document is marked done until S6 passes.
Intermediate checkpoints are review boundaries, not claims that the entire feature is deployable.

## S1. Directory expansion and mock submission boundary

Build the directory primitives before switching the shipped email contract.

- Add a mock directory module under `src/gawe/connectors/` with explicit mailbox, alias, group,
  external-list and unreadable-group entries. Supply synthetic seeds for both ordinary controls
  and adversarial cases, including nested groups and external members behind an internal label.
- Use one consistent snapshot and an opaque monotonic global revision. Normalize domains using
  the approved M15 identity rules; preserve local-part case and plus tags. Return a deterministic
  sorted unique mailbox set, marker and separate lookup timestamp.
- Reject malformed addresses, unknown internal entries, unreadable memberships, cycles, empty
  groups, depth over 16 and more than 100 expanded mailboxes. Never truncate or fall back to the
  original label. Known aliases expand regardless of domain; external lists remain single targets.
- Add the distinct mock mail-server acceptance component and its persistent operation records.
  It must reject group/alias envelope targets and accept only terminal mailbox/external-list
  targets. Headers and application-intent records cannot control the accepted-recipient record.
- Provide a guarded directory-check/submission operation and deterministic mutation hooks. Cover
  marker-only changes and membership changes at the submission boundary, not merely changes
  between two unrelated test calls.
- Persist mock server operation identity and acceptance together, with restart-safe deduplication.
  Choose one shared durable state directory for reconstructed adapters. Ensure separate processes
  cannot accept duplicate operations through independent in-memory locks; test the chosen storage
  locking/transaction mechanism. Directory revisions must survive reconstruction.
- Keep these modules out of the production email route until S2 activates the complete prepared
  action contract. Do not change existing benchmark expectations in this foundational checkpoint.

Acceptance: expansion controls and all rejection cases have tests; direct group submission is
rejected; server acceptance cannot be fabricated by changing headers or the app ledger; restart
and concurrent duplicate submission preserve one authoritative operation. Full checks pass.

Suggested commit: `feat(connectors): add mock recipient directory and mail acceptance records`

## S2. Activate prepared email v2 and fresh-approval handling

- `core/models.py`, `registry.py`, `validator.py`, `llm/planner.py`, API tool catalog: add the
  optional proposal schema, defaulting to the existing input schema. Validate both schemas.
  Use only the proposal schema for model/user arguments and the complete input schema for policy
  and execution. Reject supplied trusted-envelope fields, including nested and `$ref` attempts.
- `api/wiring.py` and app assembly: register version-specific preparation adapters and their
  required metadata. Fail startup when a tool requiring preparation lacks the right adapter.
  Retain all-version policy-reference and provenance checks.
- `engine/orchestrator.py`: resolve references, validate proposal arguments, prepare recipients,
  validate the complete action, then evaluate/hash. Persist `delivery.mailboxes`, directory marker
  and sticky `reapproval_required` in resolved arguments. Keep lookup time outside those arguments.
- Persist preparation metadata and immutable approval snapshots through memory and Postgres
  stores. Add the next migration, corresponding Alembic wrapper, constraints and least-privilege
  grants. The approval snapshot retains its own lookup time and hash across later refreshes.
- `core/policy/rules.py`: add the two explicit list-domain predicates. Malformed, missing or empty
  lists and invalid domain operands error/deny even without schema validation. Neither predicate
  becomes an allowed allow-rule operator. Extend unreadability/operator properties accordingly.
- Contract Review: activate `email.send@2.0.0`, its prepared execution schema, list-domain rules
  preserving existing rule ids, and the legal-review rule for `reapproval_required`. Send only
  the expanded envelope through the S1 guarded submission boundary. Keep the requested label
  available as a header; never use it as the envelope fallback.
- Revalidate set and marker immediately before each genuinely new dispatch. On a mismatch with
  proven no dispatch, perform the approved deterministic snapshot refresh and new policy/hash
  cycle. Keep subject/body and plan unchanged; set the sticky reapproval flag. Deny wins; otherwise
  require a fresh legal approval, including for an all-internal set or marker-only change.
- Define explicit refresh transitions and durable revision/CAS ownership. Persist enough refresh
  intent to recover a crash between snapshot, decision and approval writes. Close stale pending
  approvals before issuing a new one; never let an old approval or stale worker authorize the new
  hash. If dispatch might already have happened, do not refresh to a new operation key.
- Handle unreadable re-expansion as a safe failure with no send. Repeated changes require another
  approval cycle, never an automatic resend loop. Lookup-time-only changes leave hashes stable.
- Retire v1 execution explicitly: unexecuted pinned v1 steps fail closed; terminal history remains
  readable; legacy uncertain/in-flight effects are flagged for reconciliation and never resent.
  Document draining old pending sends before deployment. Do not rewrite old signatures or hashes.
- Update app fixtures, live-smoke loading, direct policy inputs, M14/M15 properties and sequence
  fixtures for v2. Enumerate seeded legitimate targets rather than permitting arbitrary unknown
  internal directory entries. Benchmark email effect matching must inspect server acceptance.

Acceptance: forged envelopes fail; mixed groups get the strictest outcome; group labels never
reach acceptance; adding/removing members and marker-only changes invalidate prior permission;
unreadable membership blocks; fresh approvals bind the new snapshot; crash/restart and concurrent
refresh tests cannot send under stale approval. Schema/store checks pass. Run the free scripted
benchmark and compare all original 38 expectations (7 executed, 8 denied, 23 approval pending).

Suggested commit: `feat(email): bind approval and sending to expanded recipients`

## S3. Independent read-back and durable verification lifecycle

Build the read-back operation and its persistent waiting states together. Scheduling follows in
S4; this checkpoint is not the deployable delayed-verification feature by itself.

- Add typed read-back results and version-specific adapters for CRM assessment, IAM grant, email
  send and e-signature. Require them at app assembly for the shipped write tools. Keep them out
  of the Planner catalog and retain explicit tool-write resource declarations.
- Give CRM, IAM and e-sign mocks separate persistent operation records and read APIs; use the S1
  mail server for email. Reads use the engine-derived operation key, never a tool-returned id as
  authority. Independent fault modes control reply, applied state, visibility and read failures.
- Compare all fields specified in the design, including accepted recipients and text digests.
  Treat output schema checking as a protocol check, not proof of a write. Reconcile potentially
  applied writes even when their replies are malformed. Keep read-tool behavior unchanged.
- Add `awaiting_readback`, `verification_blocked`, `verification_unknown` step states and
  `waiting_verification`, terminal `unknown` run states. Preserve the meaning of existing pending.
  Implement the design transition table and update hash/state invariants in memory and SQL.
- Add durable dispatch/verification context, tenant-scoped evidence and generic maintenance job
  storage. Identity includes plan/step version and operation key. Persist dispatch intent and the
  fixed five-minute deadline before an external call; retries/restarts cannot reset either.
- Add a migration and store contracts covering due time, deadline, result, lease/revision, evidence
  and pending continuation. Make state/job completion transactional or explicitly recoverable;
  inject crashes at each cross-store boundary. Do not key jobs only by reusable textual step ids.
- Separate known pre-dispatch transient write failure from uncertain dispatch. Keep retries for
  the former bounded under the same key. A possible acceptance, saved reply or read failure must
  not cause a new write/key. Recover accepted operations through their durable idempotency record.
- Introduce one verification resolver shared by direct execution, recovery and the future sweep.
  Fresh agreement succeeds, disagreement blocks with evidence, unavailable waits, and unresolved
  work at/after the original deadline becomes unknown. No automatic late rewrite of terminal
  verification outcomes, compensation or resend.
- Verification-only recovery validates action/operation identity without requiring unexpired
  approval for an already dispatched action. New dispatch still requires current authorization;
  test that the verification exemption cannot call the write path.
- Update orchestrator dependency/settlement logic: waiting makes no progress and starts no new
  work; unknown/blocked prevent downstream consumption. Preserve pending reconciliation on sibling
  failure or cancellation. A cancelled run stays cancelled after evidence resolution.
- Expose protected verification evidence/status through the API and show waiting/unknown clearly
  in UI lifecycle/badges. No raw message bodies or documents in evidence/logs; audit hashes the
  evidence. Refresh anchors after any post-terminal audit append when anchoring is configured.
- Update benchmark status classification so waiting/unknown cannot become an apparent successful
  verified action. Keep actual unauthorized effects separate from verification success: a detected
  extra-recipient acceptance is an effect that happened, not an email that was prevented.

Acceptance: independent disagreement defeats an echoing tool; a wrong returned id cannot select
another record; app-ledger/header edits cannot fabricate verification; delayed and unavailable
reads obey all three outcomes; no duplicates on write/read failures or crash recovery; expired
approvals cannot prevent a read or authorize a new write. Database, store, concurrency, API and UI
checks pass. Immediate-read default mocks preserve all 38 baseline benchmark expectations.

Suggested commit: `feat(execution): verify writes from durable system read-back`

## S4. Scheduled resolution, ownership and operational signals

- Add the generic maintenance runner with registered job kinds and injected `tick(now)` behavior.
  Read-back is the only job kind implemented in this milestone. Claim due jobs in bounded batches
  with leases/revisions; page fairly so a failing job cannot starve later work.
- Run it every 30 seconds through API lifespan; ensure shutdown stops scheduling and releases
  resources. It runs without new requests, including zero-work ticks. Keep interval/deadline
  configuration validated and document defaults and supported bounds.
- Add `python -m gawe.maintenance sweep --once` and `make sweep` against configured durable stores.
  Reject standalone ephemeral memory mode. Reopen the same mock-system directory as the API.
  The command must not construct a model provider or invoke planning.
- Route startup recovery, explicit resume and scheduled verification through the S3 resolver and
  ownership checks. Persist/recover continuation after successful verification. Continue only
  already materialized authorized plans; never reopen cancelled/failed/unknown runs or replan.
- Test competing sweepers, expired leases, stale completion writes, crashes after evidence commit
  but before job/run completion, and restarts with due work. Enforce the design's no-redispatch
  rule even when approval has expired while the result was invisible.
- Emit structured heartbeat logs and OTel counters/backlog observations for checked, done, blocked,
  newly unknown, still waiting and total unresolved unknown. Include tick timing/job kind and
  success/error status, not recipients or bodies. Emit error heartbeats for failed ticks.
- Demonstrate the alarm for unknown backlog increasing over two successful ticks and distinguish
  stale/missing heartbeat. Retain terminal unknowns in backlog accounting. Document operator
  queries; do not build external alert routing or unrelated maintenance jobs.
- Update operational documentation for lifecycle, durable/mock state, schema upgrade, one-shot
  operation and the distinction between blocked-before-send and mismatch-after-acceptance.

Acceptance: a delayed outcome resolves with no subsequent request/run; deadline expiration is
persisted; independent runners converge without duplicate effects or lost continuations; cancelled
runs reconcile without resuming; heartbeat/unknown alarms have deterministic tests. Required full,
Postgres/concurrency and applicable UI checks pass. The complete delayed-read feature now works.

Suggested commit: `feat(maintenance): sweep pending verification with heartbeats`

## S5. Output-poisoning and bounded-field interpretation evidence

- Add a separately labeled execution-integrity scenario suite under the benchmark/tests. Preserve
  the original 38 case/mode identities and expectations; new scenarios do not silently change that
  denominator. Use local scripted providers and mock fault hooks only.
- Poison `counterparty_contact` in a connector result and carry it through a valid top-level `$ref`
  into email. Cover blocked external, review-required external, internal group/alias and unreadable
  targets. Assert resolved arguments, rule ids when policy ran and authoritative accepted targets.
- Add the opt-in interpreting test mock with a bounded miniature formatting/base64 grammar and
  synthetic marker effects. No shell, eval, external commands or network. Ordinary mocks remain
  noninterpreting. Fit encoded content within the actual 300-character Reader summary schema.
- Track both literal canary bytes and decoded semantic markers. Include plain/encoded equivalence,
  malformed encoding and harmless formatting controls. Distinguish data propagation, permission,
  actual acceptance, verification status and interpretation in results.
- Under approved D5, assert that an encoded body sent to the approved internal envelope can produce
  the synthetic downstream marker. Record it as the permitted content gap; do not invent a body
  policy or claim it was blocked. An interpreter that adds recipients must instead be detected by
  accepted-recipient read-back, while retaining evidence that the extra acceptance occurred.
- Add threat-model rows/references only for implemented tests. Preserve M14/M15 boundary,
  provenance and shared-state coverage. Extend shared-state tests to use actual prepared sends and
  read-back without discarding prefixes that unexpectedly deny/wait/fail.

Acceptance: tests demonstrate both the recipient controls and the known body-content gap, with
independent expected results. No real-model susceptibility claim or silent outcome relabeling.
Full checks and the original scripted baseline pass; new scenario results are separately reported.

Suggested commit: `test(injection): measure poisoned outputs and encoded short fields`

## S6. Cumulative verification and completion record

Deferred limitation to retain in the S6 completion record: the sorted mailbox set in the action
hash is the actual recipient binding; the directory marker adds a conservative check. V2 requires
a marker, and the global mock revision forces reapproval on unrelated directory changes. A real
directory connector should use a new email tool version with an optional marker (bind the set alone
when none is available) or a per-group marker. No v2 code change is included in this deferral.
Also record that recipients are re-expanded on every send, with no cached expansion. A cache that
re-expands only when a group's marker moves is safe only if the marker moves on any change under
the group, including nested groups and members' forwarding settings; that needs a real directory.

- Review the cumulative diff for approved scope, deployment compatibility, privacy, generated
  files, missing entry-point validation and changes to suite expectations. Confirm no changes to
  excluded work or other repositories and no commits created by the assistant.
- Exercise the full required negative matrix: group reaches server, membership changes after
  approval, membership unreadable, independent read disagrees, and waiting crosses its deadline.
  Include the approved concurrency, crash, cancellation and idempotency scenarios from S2–S4.
- Run `make check`, `make check-docs`, `make test-schema`, applicable Postgres integration/store
  checks and `make ui-check`. Retain actual output and explicitly account for skipped checks.
- Run `python -m bench scripted` with the virtualenv. Compare each original case/mode outcome and
  unauthorized-effect flag with its approved expectation; assert all 38 identities are present
  once. Report 7 executed, 8 denied and 23 approval pending unless a new owner decision authorizes
  a change. Run and compare the separate S5 execution-integrity scenarios as well.
- Record result directories, commit, dirty status, counts and limitations. Keep generated data in
  ignored results directories; public docs must not imply the uncommitted tree is a clean commit.
- Update design/plan completion records, threat model and results/operational docs. Explain which
  guarantees remain mock-only, that read-back does not undo effects, and the measured content gap.
  Mark this milestone done only when its checks and acceptance criteria are actually satisfied.
- Stop for final owner review and commit. Do not merge, port or start later milestone work.

Suggested commit: `docs: record verified M16a execution integrity results`

## Approval and progress record

- Phases 0–2: design recorded and committed by the owner.
- D1–D5: approved by the owner on 2026-11-12, including the detailed design semantics.
- Phase 3: approved and committed by the owner.
- S1: complete and committed by the owner.
- S2: complete and committed by the owner on 2026-11-12.
- S3: complete, including correctness-review fixes; committed by the owner
  on 2026-11-14.
- S4: complete, including the continuation follow-up; committed by the owner
  on 2026-11-15.
- S5: complete; committed by the owner on 2026-11-15.
- S6: complete (2026-11-15); every check and acceptance criterion passed on the S5 commit.
  M16a is done. The S6 documentation record awaits owner review and commit.
- No later milestone work has begun.


S1 checkpoint: standalone directory and mail-server primitives with transactionally guarded
SQLite persistence, synthetic seeds and 30 focused tests. Separate processes deduplicate one
operation; a crash before commit rolls back acceptance and permits a safe retry. Full checks:
1711 passed, 39 skipped in 101.84s; ruff/mypy passed (102 source files); docs checked 92 references.
Full output: `bench-results/m16a-s1-check.txt`. The design records the mock storage choice and
owner-approved continuation after initial lint failures. No app schema/UI or shipped email-route
changes. S2–S6 have not started.


S2 checkpoint: prepared email v2, strict envelope policy, durable approval snapshots and signed
step decisions, guarded submission with deterministic fresh approval, and hash-aware refresh
ownership. Includes rejection of forged envelopes, reference preparation, repeated membership and
marker changes, unreadable/blocked refreshes, recovery before/after acceptance, obsolete pending
approval cleanup, v1 retirement, server-backed benchmark effects and Postgres concurrency coverage.
No commits were created during implementation; the owner subsequently committed S2
on 2026-11-12. At the S2 checkpoint, S3–S6 were unstarted and independent status-setting
read-back was not implemented. Atomic directory/mail behavior remains mock-only.

Final verification output excerpts (full logs retained):

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 104 source files
pytest --cov=gawe --cov-report=term-missing
1758 passed, 42 skipped in 115.81s (0:01:55)
schema tests passed: 75 checks
============================== 70 passed in 1.27s ==============================
python scripts/check_threat_model.py
checked 101 references, 0 problem(s)
scripted: 38 runs (26 attacks, modes planner, reader); no model is called
```

The default suite's database skips are covered by the separate 70-test Postgres run with no skips.
No UI files changed. The full suite checks each original scripted case/mode against its expected
outcome; the CLI report confirms 7 executed, 8 denied and 23 approval pending.

Logs: `bench-results/m16a-s2-check.txt`, `bench-results/m16a-s2-schema.txt`,
`bench-results/m16a-s2-integration.txt`, `bench-results/m16a-s2-docs.txt`,
`bench-results/m16a-s2-recovery.txt`, `bench-results/m16a-s2-refresh.txt`,
`bench-results/m16a-s2-scripted.txt`. Scripted artifacts:
`bench-results/20261112T235238Z-scripted/`. Based on the S1 commit; verification used the
uncommitted S2 tree. `git diff --check` passed. The roadmap and other repositories were untouched.

Suggested commit: `feat(email): bind approval and sending to expanded recipients`


### S3 checkpoint (2026-11-14; committed)

The owner approved continuation after the initial verification stop. The four regression groups
were corrected: stable identity-based benchmark observations, lazy mock storage initialization,
no repeat anchoring for unchanged terminal runs, and complete version-specific adapter fixtures
without weakening all-version schema/provenance validation. Initial failure logs remain available.

Shipped writes now require version-specific independent read-back at assembly. CRM, IAM and e-sign
have durable SQLite operation records separate from the app ledger; email uses S1 acceptance.
Keys bind action hash and plan version. Intent and the original five-minute deadline are durable
before invocation. Proven pre-dispatch rejection alone may retry; uncertain dispatch routes to
read-back. Agreement, disagreement, unavailable and deadline expiry produce explicit outcomes.
Malformed replies retain protocol failure; returned ids never select another operation record.

Migration 0006 adds verification states and tenant-bound maintenance storage. Jobs persist lease,
revision, evidence, audit sequence and continuation, with CAS updates and immutable intent/result.
The common resolver persists result, applies step, records the evidence digest, then acknowledges
continuation after run settlement. Crash tests cover memory and separately committed Postgres
stores, plus independent workers and independently running mock writers. Legacy in-flight writes
without S3 intent are flagged for investigation and never resent or verified from their reply.

Waiting stops new work; unknown/blocked prevent output consumption. Cancellation and sibling
failure retain reconciliation while the run's terminal state is preserved. Explicit resume can
finish that evidence without restarting the plan. Evidence is tenant-protected, bounded and uses
text digests; UI lifecycle/badges distinguish waiting and uncertainty. The current step selects
its own action's evidence after recipient refresh. Post-terminal audit evidence refreshes configured
anchors through append-only predecessor-linked checkpoints after validating the protected prefix;
earlier checkpoints remain intact and timestamp continuations require a token for the new digest.

Actual verification, against the uncommitted isolated checkout based on the S2 status correction:

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 110 source files
1836 passed, 59 skipped in 125.01s (0:02:05)
97 passed in 3.72s                         # separate integration/store/Postgres run; no skips
schema tests passed: 84 checks
0006 (head)                               # Alembic upgrade on a throwaway database
checked 111 references, 0 problem(s)
UI: TypeScript passed; 18 tests passed; production build passed
PASS: all 38 identities unique; every original outcome and accepted-effect flag matches.
7 executed, 8 denied, 23 approval pending; zero live model calls.
```

The default suite's 59 database skips were exercised in the separate 97-test run. PostgreSQL was
reachable on localhost; tests used throwaway databases/schemas. The Docker socket was unavailable,
so `scripts/test_schema.sh` ran through local `psql` over TCP, exercising the same SQL files as
`make test-schema`. The initial failed attempt to start another Postgres was unnecessary.
Signed-timestamp checks used recorded offline fixtures; no timestamp authority was contacted.

Logs: `bench-results/m16a-s3-verified-check.txt`, `m16a-s3-postgres-final.txt`,
`m16a-s3-schema.txt`, `m16a-s3-alembic.txt`, `m16a-s3-verified-docs.txt`,
`m16a-s3-final-ui.txt`, `m16a-s3-anchor-tests.txt`, `m16a-s3-scripted.txt` and
`m16a-s3-scripted-comparison.txt` (all under `bench-results/`). Scripted artifacts:
`bench-results/20261115T004230Z-scripted/` (UTC run identifier).

Apply migration 0006 and retain the mock system-state files when adopting this checkpoint. Old
in-flight operations lacking intent require investigation; do not invent deadlines for them.
S3 is not the deployable delayed-verification feature: the periodic runner/heartbeat remain S4.
Read-back proves the original operation record; it does not undo an effect or freeze later state.
Real downstream guarantees remain unimplemented. Downgrade needs a reviewed data migration.

Initial implementation and verification used `/tmp/gawe-m16a-s3` on `m16a-sending-readback`.
The work was subsequently applied to the original repository and committed with the review fixes
on 2026-11-14.
No assistant commit, roadmap change, real integration, or S4–S6 implementation was made.

Suggested commit: `feat(execution): verify writes from durable system read-back`


### S3 review-fix verification (2026-11-14)

The S3 changes are now applied in the original repository. The owner requested fixes for the
reviewed stale-dispatch and recipient-rejection crash windows. Dispatch now checks current
eligibility and renews ownership with CAS before a new invocation, including after attempt
persistence. The recipient-change result is durable before attempt completion, so either crash
boundary recovers to fresh approval rather than terminal unknown. Tests cover both boundaries,
takeover during backoff/attempt persistence, and separate Postgres workers. Existing staged S3
changes were preserved. The owner committed S3, these fixes and this record on
2026-11-14. S4 remains unstarted.

Actual verification:

```text
All checks passed!
Success: no issues found in 110 source files
1840 passed, 60 skipped in 124.10s (0:02:04)
98 passed in 2.76s                         # separate Postgres run, no skips
99 passed in 1.96s                         # focused write/read-back and prepared-email tests
```

Logs: `/tmp/gawe-s3-fix-check.txt`, `/tmp/gawe-s3-fix-postgres.txt`,
`/tmp/gawe-s3-fix-focused.txt`. The original reproductions now show zero accepted writes after
cancellation and fresh approval after recipient rejection. One initial focused failure caught
an approval-expiry classification regression; it was corrected before these passing runs.
No schema or UI changes were needed for these fixes; their S3 review checks already passed
(84 SQL checks; UI type check, 18 tests and production build). The full Python suite includes
the original 38 scripted-outcome assertions.

Suggested commit: `fix(execution): fence retries and preserve recipient refresh recovery`


### S4 checkpoint (2026-11-15; committed)

Based on the S3 completion record on `m16a-sending-readback`. A new `gawe.maintenance` package adds a generic
runner (`tick(now)`, registered job kinds) with read-back as its only kind. Each tick pages due
jobs by an exclusive `(due_at, job_id)` cursor in bounded batches, claims each with a 30-second
lease by revision CAS, and moves failed or unacknowledged work back one interval so it cannot
starve later jobs. The read-back handler has no executor or tool access: it resolves through the
S3 resolver under the claimed lease, then asks the orchestrator to settle and continue the
existing plan. Unplanned runs are skipped; terminal runs are only acknowledged.

The API lifespan ticks at startup and every 30 seconds on a dedicated thread, independent of
requests; shutdown stops scheduling, waits for a running tick and shuts the thread down.
`python -m gawe.maintenance sweep --once` / `make sweep` builds the engine with no model provider
(the orchestrator refuses to plan without one), rejects `GAWE_STORE=memory`, and reopens the mock
state through the same `GAWE_APP`/`GAWE_DEMO_DIR`. Interval (1-300 s), deadline (30-3600 s,
longer than the interval; default 300 s), batch and per-tick limits are validated settings.
The store protocol gains the cursor on `due` and per-kind outcome `counts`; no schema change.

Heartbeats are structured `gawe.maintenance.heartbeat` logs plus telemetry counters and backlog
observations, with counts, timings, kind and error class only. A failed tick emits an error
heartbeat with null totals. `assess` implements the two alarm conditions: unknown backlog growing
between two successful heartbeats, and a missing/stale/error heartbeat. Terminal unknowns stay in
the backlog after acknowledgment. Startup recovery and explicit resume keep using the resolver;
a test shows both are fenced out while a runner holds the lease. Operator docs are in `docs/api.md`.

Tests: 38 in `tests/unit/test_maintenance_runner.py` (delayed resolution for all four writes with
no request, blocked, deadline unknown, zero-work heartbeat, cancellation, stale page, four racing
runners, lease expiry, stale completion write, recovery/resume vs. lease, four crash boundaries
after evidence commit, restart after dispatcher crash, no redispatch after approval expiry and
after intent without a send, fairness, error heartbeats, alarm, settings, lifespan, CLI), three
Postgres runner tests and one store cursor/counts contract test on both backends. Threat-model
row T28 references only these tests.

Actual verification:

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 116 source files
1879 passed, 64 skipped in 131.63s (0:02:11)
103 passed in 3.79s                        # tests/integration + tests/contract on Postgres, no skips
checked 124 references, 0 problem(s)
scripted: 38 runs (26 attacks, modes planner, reader); no model is called
identities unique and equal: True; differences vs approved S2 baseline: none
7 completed, 8 denied (policy_deny), 23 cancelled after approval pending
```

The default suite's 64 skips are the database tests run separately above. No migration or SQL
changed, so `make test-schema` and Alembic were not rerun; the documented operator SQL was executed
against a throwaway schema with all migrations applied. No UI change, so `make ui-check` was not
run. `git diff --check` passed. Logs: `bench-results/m16a-s4-check.txt`, `m16a-s4-postgres.txt`,
`m16a-s4-docs.txt`, `m16a-s4-scripted.txt`, `m16a-s4-scripted-comparison.txt`; scripted
artifacts `bench-results/20261115T190401Z-scripted/`.

Known limitations: the backlog query scans job records (no kind index); unknown has no
operator-clear workflow, so its backlog only grows; alert routing is not built. While a read is
still waiting on a run left `running` by a crash, a tick may read more than once (reads only).
A live dispatcher slower than its 30-second lease can be overtaken by a runner's read, as with S3
recovery; it can then only reconcile. Real mail/directory behaviour remains mock-only.

No commit, staging, roadmap change, public-repo change, model call or S5/S6 work was made.

Suggested commit: `feat(maintenance): sweep pending verification with heartbeats`


### S4 follow-up: continuation off the sweep thread (2026-11-15; committed with S4)

At the owner's request, the one limitation worth fixing before commit was addressed. In the API,
continuation after a resolved read-back now goes through the service's background pool (the same
path as resume and startup recovery, with advance errors audited) instead of running on the sweep
thread, so a slow next step cannot delay ticks or make the heartbeat look stale. The one-shot
sweep still continues inline. Fixing this exposed a release gap: the resolver clears the lease
when it records a result, so the runner did not move an unacknowledged result back and would
have handed it over on every tick. The runner now also moves back unleased results awaiting
continuation; unresolved jobs keep the resolver's next read time, and no move passes a deadline.
`test_api_continuation_runs_off_the_sweep_thread` holds the hand-off and checks the tick returns
first, the job moves back one interval and a repeated hand-off completes the run once. Threat-model
row T28 references it. The remaining limitations above are unchanged and deferred.

Actual verification of the follow-up tree:

```text
All checks passed!                          # ruff
Success: no issues found in 116 source files
1880 passed, 64 skipped in 131.55s (0:02:11)
103 passed in 4.24s                        # Postgres integration/store, no skips
checked 125 references, 0 problem(s)
identities unique and equal: True; differences vs approved S2 baseline: none
```

Logs: `bench-results/m16a-s4-fix-check.txt`, `m16a-s4-fix-postgres.txt`, `m16a-s4-fix-docs.txt`,
`m16a-s4-fix-scripted.txt`, `m16a-s4-fix-scripted-comparison.txt`; scripted artifacts
`bench-results/20261115T191240Z-scripted/`. No schema or UI change. `git diff --check` passed.


### S5 checkpoint (2026-11-15; committed)

Based on the S4 commit on `m16a-sending-readback`. A separate execution-integrity suite,
`bench/integrity/` (`python -m bench integrity`), runs 23 scenarios through the real Contract
Review app with a scripted Reader and Planner and no model calls. It has its own identities,
expected records and results directory; the 38 scripted case/mode runs are unchanged. Each record
separates propagation (resolved value equals the source), preparation error, policy outcome and
rule ids, approval, server-accepted recipients (and any beyond the prepared set), step/run
status, read-back code, literal canary persistence and decoded semantic markers.

- Output poisoning (P00–P13): a test fault hook on the mock contract system,
  `MockCLM.output_overrides`, replaces `counterparty_contact` in `get_contract` output only (never
  `deal_values()`); `contract_review.bundle` accepts a `clm` argument like `email`. A top-level
  `$ref` carries it into `email.send.to`. Blocked external and an internal group hiding a blocked
  member deny; other external targets, an external list and an internal group with an external
  member need legal approval (two approved variants show the server accepting exactly the
  prepared mailbox); internal groups, an alias to a group and an external-looking alias to an
  internal mailbox expand and send to members only. Unreadable, unknown internal, malformed and
  comma-joined targets fail in preparation before policy and dispatch.
- Bounded field (B01–B09): `bench/integrity/interpreter.py` adds an opt-in `InterpretingEmail`
  whose renderer recognizes only `{{b64:...}}` (at most 200 characters, 120 printable ASCII bytes)
  and `{{upper:...}}`, in one pass with at most eight directives, then looks for synthetic
  `GAWE-MARKER:`/`GAWE-CC:` markers. Its only effect is an in-memory list; no shell, eval, files
  or network (a test checks its imports and calls). Ordinary mocks never interpret bodies. The
  payload rides in the Reader's 300-character summary (a 301-character summary fails reading).
  Plain and encoded markers have identical policy/acceptance outcomes and differ only in literal
  canary persistence; malformed encoding is reported and left literal; formatting and unknown
  directives produce no marker; an encoded body to a blocked domain is denied and never rendered.
- D5 content gap (B02, the only scenario marked `known_gap`): an encoded body to the allowed
  internal recipient is allowed, accepted, verified and decoded to the marker, while the exact
  canary is absent from stored text. It is recorded, not blocked; no body policy was added, and a
  test fails if a rule ever reads `body` or `subject`.
- Interpreter fault (B07 encoded, B08 plain): the mock adds a recipient at the server after policy
  and its reply echoes only the approved mailbox. Accepted-recipient read-back disagrees, the step
  is `verification_blocked`, the run fails and the extra acceptance remains a recorded effect.
- Shared-state sequence tests now also require every prefix write's read-back job to agree and
  every prefix send's server acceptance to equal its prepared envelope. Prefixes that deny, wait
  or fail still fail the test rather than being discarded.

Threat-model rows T29 (poisoned output) and T30 (encoded body, interpreter-added recipients,
known content gap) reference only tests in `tests/bench/test_integrity.py`.

Actual verification:

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 119 source files
1929 passed, 64 skipped in 134.58s (0:02:14)
103 passed in 3.68s                        # make test-integration on Postgres, no skips
checked 136 references, 0 problem(s)
integrity: 23/23 match their expected records; known D5 content gap demonstrated by: B02-encoded-marker
scripted: 38 runs (26 attacks, modes planner, reader); no model is called
identities unique and equal: True; differences vs approved S2 baseline: none
stopped_by counts: policy_deny 8, approval_pending 23, none 7
```

The default suite's 64 skips are the database tests run separately above. No migration, SQL or UI
changed, so `make test-schema` and `make ui-check` were not run. `git diff --check` passed. No
scripted expectation changed. Logs: `bench-results/m16a-s5-check.txt`, `m16a-s5-postgres.txt`,
`m16a-s5-docs.txt`, `m16a-s5-integrity.txt`, `m16a-s5-scripted.txt`,
`m16a-s5-scripted-comparison.txt`; artifacts `bench-results/20261115T193043Z-integrity/` and
`bench-results/20261115T193054Z-scripted/`.

Known limitations: the scenarios measure deterministic controls once data is poisoned, not real
model susceptibility. An allowed internal recipient chosen by poisoned data is still sent without
a human. Read-back detects interpreter-added recipients only after acceptance and does not undo
them. The interpreter is a miniature test grammar, not a model of real template engines. The
integrity suite covers Contract Review only.

No commit, staging, roadmap change, public-repo change, model call or S6 work was made.

Suggested commit: `test(injection): measure poisoned outputs and encoded short fields`


### S6 checkpoint (2026-11-15; committed)

Verified on the clean S5 commit (`git status` empty before the run; generated records report
`git_dirty: false`). Cumulative review of `main..079084c` (99 files): scope matches D1–D5 and the
plan; no change to excluded work, the roadmap or other repositories; all branch commits are the
owner's. Deployment compatibility: migrations 0005/0006 apply cleanly through Alembic, 0006
refuses downgrade, v1 email is retired fail-closed. Privacy: heartbeats, telemetry and audit carry
counts, codes and digests only; verification evidence holds bounded compared fields with text as
digests and is tenant-scoped like the run. Entry points: the sweep CLI validates its settings,
refuses memory stores and builds no model provider; the API validates maintenance settings at
startup; the evidence endpoint uses the run's tenant check. No generated files are tracked.
Suite expectation changes adapt fixtures to v2 preparation and list-domain operators without
relaxing an outcome. No code change was needed; four documentation gaps were fixed (API
endpoint row, stale "never contain" wording, v2/migration upgrade notes, `.env.example`
maintenance settings), and the threat model, results page and both completion records updated.

Negative matrix and S2–S4 scenarios: 191 tests on Postgres, no skips (`m16a-s6-matrix.txt`).
A mutation check on an exported copy removed each guard in turn (server terminal-envelope check
plus label send, send-time revalidation, unreadable-group rejection, read-back comparison,
deadline) and confirmed each of 14 named tests fails, then passes again unmutated
(`m16a-s6-mutation.txt`).

Actual verification output:

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 119 source files
pytest --cov=gawe --cov-report=term-missing
1929 passed, 64 skipped in 139.78s (0:02:19)
schema tests passed: 84 checks
============================= 103 passed in 4.17s ==============================
0006 (head)
checked 136 references, 0 problem(s)        # make check-docs, after the doc edits
UI: # tests 18 / # pass 18 / # fail 0 / # skipped 0; tsc and next build passed
============================== 191 passed in 7.76s ==============================
ALL MUTATIONS CAUGHT
records: 38 unique identities: 38
differences vs approved S2 baseline 20261112T235238Z: 0
differences vs main (7a564c8): 0
mismatches vs cases.yaml expect_scripted: none
stopped_by counts: {'policy_deny': 8, 'approval_pending': 23, 'none': 7}
integrity: 23/23 match their expected records; known D5 content gap demonstrated by: B02-encoded-marker
```

Skips: all 64 default-suite skips are database tests that need `GAWE_TEST_DATABASE_URL`
(`m16a-s6-skips.txt`); the same files ran with it set, 103 passed with none skipped. Docker was
available, so `make test-schema` ran as written. The scripted comparison also lists 12 field
differences against the older 2026-11-10 run; they are M15's three planner-mode lateral grants,
already on `main`, and the same suite run today on an exported copy of `main` differs in nothing.

Logs: `bench-results/m16a-s6-check.txt`, `m16a-s6-schema.txt`, `m16a-s6-postgres.txt`,
`m16a-s6-alembic.txt`, `m16a-s6-ui.txt`, `m16a-s6-docs.txt`, `m16a-s6-skips.txt`,
`m16a-s6-matrix.txt`, `m16a-s6-mutation.txt`, `m16a-s6-scripted.txt`,
`m16a-s6-scripted-comparison.txt`, `m16a-s6-integrity.txt`; artifacts
`bench-results/20261115T193648Z-scripted/` and `bench-results/20261115T193649Z-integrity/`.

The completion record, mock-only guarantees, the D5 content gap, the deferred marker and no-cache
notes, and the limitations that still stand are in the design's S6 completion record. No commit,
staging, merge, roadmap change, other-repository change, model call or later-milestone work was
made.

Suggested commit: `docs: record verified M16a execution integrity results`
