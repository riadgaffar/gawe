# M16b build plan

Status: **Done.** S1–S6 approved and committed by the owner (2026-11-16). Plan approved by the owner on
2026-11-15.
The owner approved D1–D6 in [m16b-design.md](m16b-design.md) on 2026-11-15, as recommended.
This plan implements those decisions. Approve-once (D5) is split into its own milestone and has
no step here.

## Working rules and verification

Work only on `m16b-approval-integrity`. Preserve the owner's changes. Do not commit, stage or
push; the owner reviews and commits each checkpoint. Do not edit the roadmap or other
repositories. No live model calls and no real connectors. Do not build approve-once, a ticket
system, undo, combination rules or anything else outside D1–D6.

Run S1–S6 in order. At each checkpoint give the diff summary, the actual verification output
(with a complete local log), known limitations and a suggested one-line commit message, then stop
for owner review. If verification fails twice on the same step, stop and report rather than
continuing a repair loop. A substantive change to approved security semantics needs a new owner
decision; routine implementation details may be settled within this plan and recorded.

At each checkpoint run:

```sh
PATH="$PWD/.venv/bin:$PATH" make check
PATH="$PWD/.venv/bin:$PATH" make check-docs
```

For schema changes also run `make test-schema`, an Alembic upgrade on a throwaway database and
the Postgres store/integration tests (`make test-integration`). For UI changes run
`make ui-check`. Report unavailable prerequisites as incomplete verification; skipped database
tests are not schema or concurrency evidence. Run `python -m bench scripted` (and
`python -m bench integrity` where a step touches email, contracts or grants) and compare every
case, not just the exit code.

Use focused tests first. Keep outputs in ignored `bench-results/m16b-sN-*.txt` files. Add
threat-model rows only for tests that exist. Update the design's build record at each
checkpoint; neither document is marked done until S6 passes.

## S1. Record versions checked at the write (D1)

- Contract system mock: move contract records into a durable SQLite store in the mock state
  directory, seeded from `clm_seed.json` once, each record with an opaque revision that changes
  on every mutation. Keep CRM assessment operations **and** e-sign envelope operations in that same
  database so the revision check, the deal-value check and acceptance commit in one
  `BEGIN IMMEDIATE` transaction. Keep operation keys, read-back reads and fault hooks as in M16a.
  Add a test mutation hook (`replace_contract`) and a hook that fires between a separate
  pre-check read and the guarded write.
- IAM mock: the same for user records with revisions and grant operations in one database.
  Store the grant's `expires_at` and run id with the operation record (enforcement is S4).
- Preparers for `crm.write_assessment@2.0.0`, `esign.send_for_signature@2.0.0` and
  `iam.grant_access@2.0.0`: proposal schemas unchanged; execution schemas add a required trusted
  `record` object (`contract_version` or `user_version`); the grant also adds `expires_at`
  (preparation time plus the tenant's grant lifetime, default 7 days, validated 1 hour–30 days).
  Missing, unreadable or unknown records fail in preparation with a safe code before policy.
  A `record` or `expires_at` supplied by the model, nested or through `$ref`, is rejected.
- Manifests declare record binding; connectors declare conditional-write support. Assembly fails
  if a bound tool lacks either a preparer or that declaration, in every registered version.
- Connectors pass the bound version into the guarded write. A mismatch is a proven pre-dispatch
  rejection: step fails `record_changed`, no retry, no read-back wait, no automatic refresh.
  The e-sign tool's separate read before the write is removed.
- Read-back adapters for the v2 tools compare the bound version and (for grants) `expires_at`
  as well. v1 adapters stay registered for already dispatched v1 writes; unexecuted pinned v1
  steps fail `unsupported_tool_version`. Nothing is resent or relabelled.
- Policies keep every rule id; add an IT-access deny for a grant lifetime over the tenant maximum.
  Update provenance declarations (the record versions come from system resources no tool writes),
  M14 property and M15 boundary fixtures, sequence prefixes and benchmark matchers for v2.

Tests: a record replaced under the same id between approval and execution fails; a change
injected between a separate read and the write is refused by the conditional write; a
change-away-and-back is still refused (revision, not content); forged `record`/`expires_at`
fail; missing version fails in preparation; concurrent writers on separate processes cannot both
pass a stale version; v1 retirement and v1 read-back recovery. Scripted: all 38 expectations
unchanged (7 executed, 8 denied, 23 approval pending); integrity: 23/23.

Suggested commit: `feat(connectors): bind approvals to record versions with conditional writes`

## S2. Unreadable marker and signed decision v2 (D2)

- `PolicyDecision` gains `format` (default 1), `checks` (default empty) and `policy_digest`
  (default none). Old JSON parses as v1. A v1 decision with checks or a digest is invalid.
- Rule evaluation reports each evaluated check (`rule_id`, path, operator, `unreadable`) for
  every rule whose selectors matched, in order, stopping where evaluation stops, using the
  operator table in design §5.1. Matching is unchanged.
- `RuleEngine` computes the rule-set digest at load and issues only v2 decisions.
  `PolicySigner` seals v2 under `gawe.decision.v2\n`; verification dispatches on `format`, with
  no fallback. Executor, approvals, recovery and both stores accept v1 and v2.
- `policy.decided` audit payload adds `format`, `checks` and `policy_digest`, built by one
  function from the stored decision (S5 reuses it). The audit event format is unchanged.
- API run/step views expose checks as rule id, path, operator and boolean only.

Tests: golden v1 decisions (fixed key and JSON) verify and still authorize execution after a
restart on memory and Postgres stores; a pending v1 approval can still be decided and executed;
a recorded pre-M16b audit chain verifies; tampering with any check, the digest or the format
fails; v2 relabelled v1 fails; a property test shows outcome and rule ids identical with and
without check recording across M14's generated inputs; per-operator readability cases; no value
or operand appears in checks, payloads or API output. Scripted and integrity unchanged.

Suggested commit: `feat(policy): sign per-check unreadable markers in decision format v2`

## S3. Approval screen without model prose (D3)

- `ToolManifest` gains an argument classification (`typed` or `free_text` per leaf path of the
  execution schema). Registration fails on an undeclared leaf or an unknown path, in every
  version. Declare it for all shipped tools.
- Approval view (server side, from the approval's own snapshot): typed fields in order, the
  action hash, the difference from the most recent approved approval in the same tenant for the
  same tool name (typed fields as added/removed/changed; free-text only changed/unchanged by
  digest; "no earlier approved call"), and free-text fields as headers with path, the label
  "Written by the model from an untrusted document" and length in code points. Content only in a
  separate field the UI renders on explicit expand.
- UI: a pure `ui/lib/approval-view.ts` builds the card model; `ApprovalCard.tsx` renders typed
  fields, hash and diff first, free text in collapsed `<details>` whose summary is the label and
  length only. Remove the raw JSON block from the card.

Tests: API classification and diff (including tenant isolation and no earlier approval); a
registration failure for an unclassified leaf; UI tests that every free-text field is collapsed
and labelled, the header shows the length and contains none of the field's text, typed fields
come first. Record "does wording sway approvers" as waiting. `make ui-check`.

Suggested commit: `feat(approvals): show typed fields first and collapse model-written text`

## S4. Grants expire, enforced by the sweep (D4)

- IAM mock: a grant is inactive at or after `expires_at`; add `revoke` (idempotent, its own
  operation key) and an active-at-time read.
- A `grant_expiry` maintenance job is created with the grant's dispatch intent, before the write,
  due at `expires_at`. Its handler revokes and confirms by read-back; a grant never accepted reads
  back as absent and completes. Audit `grant.revoked` on the run, with anchor refresh.
- Heartbeat outcomes and an overdue-active count for the kind; the alarm fires when that count is
  above zero across ticks. Renewal is a new run; there is no extend operation.
- Document that grants from before M16b have no end date and need operator review.

Tests: a grant past its end date that is still active fails (mock check, missing job, revoke not
read back); revoke after restart, duplicate sweeps, lease expiry and a crash between revoke and
job completion revoke once; cancelled runs still get revoked; the alarm. Postgres runner tests.

Suggested commit: `feat(maintenance): expire grants and revoke them from the sweep`

## S5. Next-day review with a keyed sample (D6)

- Migration 0007 (SQL and Alembic, numbered after 0006): allow tenant-scoped maintenance jobs
  without a run (constrained by kind); add `review_items` (tenant, day, run, step, action hash,
  reason, status, verdict, decider, key id) with least-privilege grants; add an index on
  `audit_events (type, ts)`.
- Runner: a per-kind scheduling hook that ensures one `daily_review` job per tenant and UTC day
  with a deterministic id, due 01:00 UTC.
- Review: list the day's `policy.decided` events; rebuild each payload from the stored decision
  and match its hash; verify the signature and the action hash over stored arguments; re-evaluate
  automatic executed actions under the current rules with the run's findings and current values,
  labelling `rules_changed` (digest differs) or `inputs_changed`; v1 decisions get "rules may
  have changed"; superseded decisions are reported as such, not as tampering.
- Sample: `HMAC-SHA256(GAWE_REVIEW_SAMPLE_KEY, action_hash)` below 5% of the range, over automatic
  `crm.write_assessment` "low" ratings with no flagged clause; the key is at least 32 bytes and
  separate from the signing key; each item records the key id.
- Results: `review_items`, `review.flagged`/`review.sampled` audit events on the run, heartbeat
  counts; API list and verdict (`confirmed` or `missed_risk`) for a `legal_reviewer` who is not
  the run's requester; a small UI page.

Tests: a tampered stored decision, arguments or payload is flagged; rules and inputs changes are
labelled correctly; sample selection is reproducible with the key, changes with another key and
lands near 5% on synthetic volume; the scheduler creates one job per day across racing runners;
tenant isolation; separation of duties on verdicts. `make test-schema`, Alembic, Postgres tests,
`make ui-check`.

Suggested commit: `feat(review): add next-day audit-backed review with a keyed sample`

## S6. Cumulative verification and completion record

- Review the cumulative diff for scope, compatibility (v1 decisions and tools, migration 0007),
  privacy (no argument values in checks, heartbeats, audit or review items), generated files
  and changed suite expectations. Confirm no commits by the assistant and no other repositories.
- Negative matrix, each with a test that fails when its guard is removed (checked on an exported
  copy): a replaced record under the same id, a change between read and write, an old decision or
  audit event that no longer verifies, a tampered v2 decision, an expired grant still active,
  model text in a collapsed header, a tampered decision missed by review.
- Run `make check`, `make check-docs`, `make test-schema`, Alembic, Postgres integration/store
  tests and `make ui-check`; the scripted suite against all 38 expectations and the integrity
  suite. Update the threat model, results and API docs, and both completion records, including
  that approve-once is split out and what stays mock-only.

Suggested commit: `docs: record verified M16b approval integrity results`

## Approval and progress record

- Phases 0–2: design committed by the owner.
- D1–D6: approved by the owner on 2026-11-15, as recommended; D5 split out.
- Phase 3: this plan, approved by the owner on 2026-11-15.
- S1: approved by the owner and committed.
- S2: approved by the owner and committed.
- S3: approved by the owner and committed.
- S4: approved by the owner and committed.
- S5: approved by the owner and committed, with a seven-day catch-up added at the
  owner's request after the first review.
- S6: cumulative review, negative matrix (11 of 11 guards caught) and final verification;
  threat model, results and API docs updated; approved by the owner and committed.
- Approve-once (D5) is split into its own milestone and not built here.
