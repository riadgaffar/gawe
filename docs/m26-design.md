# M26 design: An undo record written before each action

Status: **Done.** Approved 2026-11-24; S1–S6 approved and committed by the owner and merged to `main` by fast-forward (2026-11-26).
The approved decisions are retained below; the build record describes the implementation.
No paid runs.

## 1. Purpose and boundaries

Queue item 10. r/LLMDevs readers (2026-11-13) suggested that an undo record written before an
action could make more unattended work safe, and described a trusted wrapper that saves prior
values and undoes by compare-and-swap, stopping if the current value differs. What they asked
for, in our words:

- **Trusted code saves the prior values** before an eligible change, never the model.
- **Bind undo to the original action and the record's revision after it.** Check that revision
  in the same transaction as the reversal. Something else changed since, the state can't be
  read, or the original outcome is unknown: no automatic undo.
- **Change away and back must be refused.** The agent writes B at revision 8; someone changes B
  to C and back to B at revision 10. The values match; undo bound to revision 8 must refuse.
- **The reversal is its own action:** its own policy decision, idempotency and independent
  read-back.
- **Reversibility comes from connector and policy metadata,** not model confidence. Restoring
  local state doesn't reverse what others have already seen; a refund or a delete doesn't make
  the original effect reversible.

In scope: undo for the risk rating, the mock changes it needs, the reversal path, the API and
the UI to request it, tests and docs. Out of scope: automatic (unattended) undo; changing M17's
rule that every "low" needs legal; undo for email, e-signature, replay or grants (D1); real
connectors.

## 2. Baseline before M26

- **The writes append; they don't change a record.** `crm.write_assessment@2.0.0` adds an
  operation row under its idempotency key next to the contract. The contract's revision changes
  on every mutation of the contract (M16b), and an assessment isn't one. So there is no current
  rating to restore and no revision that moves when GAWE writes. `iam.grant_access@2.0.0` is the
  same: a grant row per key, with the user record's revision unchanged.
- **An unstated assumption follows.** Two runs that each write an approved assessment against an
  unchanged contract both land. Nothing runs without approval, and T31 holds, because the
  contract didn't change. If the rating is one current field, as in most CRMs, that is a lost
  update: the second approver approved replacing the original rating, not the first run's. The
  docs don't say whether assessments are a log or a field.
- **Prior art for a record written before dispatch:** a grant's expiry job (M16b) is added in
  `VerificationService.prepare` before the read-back intent, so no dispatched grant lacks one,
  and the sweep revokes it later with read-back. Revocation is trusted maintenance with no policy
  decision, because it only narrows access.
- **Pieces undo can reuse as they are:**
  - trusted preparation binds the record revision into the hashed arguments (M16b);
  - conditional writes in one transaction, read back by key (M16a/b);
  - guarantee declarations with a conformance check for each (M22), and per-tool credentials
    (M23);
  - post-terminal audit events on a finished run (M16a, T27);
  - the approval card with typed fields first (M16b).
- **The Planner sees only the app's manifests.** A tool that isn't a manifest can't be planned.

## 3. Decisions for the owner

### D1. Which actions can be undone

| Option | What | Assessment |
|---|---|---|
| **A. The risk rating only (recommended)** | `crm.write_assessment` gets undo: restore the rating and summary it replaced | The one write whose effect is internal state that a restore really reverses. Small enough to build the whole path properly |
| B. The rating, plus "end now" for a grant | A, and a person can pull a grant's expiry job forward to now | Cheap (the expiry job and revocation exist), but it isn't undo: access already used stays used. Worth doing, as its own small item |
| C. The rating and grants as full undo | Both through the D3 path | Claims a reversal that isn't one: the readers' own requirement rules it out |

Email, e-signature requests and billing replay are declared not reversible under every option,
and a request to undo one is refused with `not_reversible`.

### D2. The current rating, and the record written before the change

| Option | What | Assessment |
|---|---|---|
| **A. A current rating on the contract; prior state bound at preparation (recommended)** | `crm.write_assessment@3.0.0` sets the contract's `risk_level` and `risk_summary` and moves its revision, in the guarded transaction. Preparation reads the current rating and summary with the revision it binds, and adds them as a trusted `prior` field inside the hash. A row in a new `undo_records` table is added with the read-back intent, like the expiry job, and becomes usable only when read-back agrees and records the revision after the write | The write is conditional on the bound revision, so `prior` is exactly what the write replaced. The approver sees the change ("high → low"), the start of roadmap item 11. GAWE's own write moving the revision also closes the lost update in section 2 |
| B. Keep the append-only log; undo marks an assessment withdrawn | The "current rating" is the latest assessment not withdrawn | No mock change, but undo would then "restore" by hiding a row, and the lost update stays |
| C. Read the prior state just before dispatch | No preparation change | The value read could change before the write. A only works because the read and the bound revision come from the same read |

Under A, writes made by 1.0.0 and 2.0.0 stay readable for verification and get no undo record.
A second run bound to the old revision now fails with `record_changed`, by design (section 5).

### D3. The reversal

| Option | What | Assessment |
|---|---|---|
| **A. A new run with one prepared step (recommended)** | A person asks to undo a step. GAWE starts a run with profile `undo`, whose single step is `crm.restore_assessment@1.0.0`, prepared by trusted code from the undo record (no Reader, no Planner). It's a conditional write: it lands only if the contract still has the revision recorded after the original write, in the same transaction. Its key derives from the original operation key, so a second request makes no second change. It's read back like any write, and a post-terminal event on the original run links to it | Reuses policy, approval, executor and read-back unchanged. Each run keeps its own audit chain |
| B. A step appended to the original run | | The run may be long finished. Reopening terminal runs touches the state machine for little gain |
| C. Direct maintenance, like revocation | No policy decision | Restoring "low" over "high" is a lowering. It needs the same rules as any rating |

Refusals, each a code with no write: `not_reversible`, `undo_unavailable` (no record, or
read-back never agreed: unknown outcomes included), `record_changed` (any later change, including
away and back), `already_undone`, `undo_of_undo` (a restore can't itself be undone in M26).
In the built API, `record_changed` is reported on the restore step after a reversal run starts;
the other undo refusal codes reject the request before a run starts.

Policy for the restore uses the rating rules, so restoring to "low" needs legal, as any "low"
does under M17. Rules may add more. If later steps in the original run used the rating's output
(a summary email), the undo card lists them and says those effects stay as they are.

### D4. Who can ask for undo

| Option | What | Assessment |
|---|---|---|
| **A. A person, through the API and UI (recommended)** | A `case_manager` in the same tenant asks, from the run page or `POST /v1/runs/{run_id}/steps/{step_id}/undo`. Policy then decides the restore; separation of duties applies to any approval as it does now | Matches the readers' first step and keeps M17's rule untouched |
| B. Also the agent | The Planner can plan undo | An attacker's document could ask for it. The reversal is never a manifest the Planner sees |
| C. Automatic undo on a later finding | The daily review or a sample triggers it | That is the unattended step. It needs volume and a decision of its own, after M26 |

### D5. What M26 does about unattended work

| Option | What | Assessment |
|---|---|---|
| **A. Nothing yet; record what a later call needs (recommended)** | M17's "every low needs legal" stays. M26 records how long each undoable write stayed undoable and why undo became unavailable, as counts | Undo without volume tells us nothing about whether unattended lows would be safe |
| B. Let automatic "low" ratings run again, with undo as the safety net | | Reopens M17's call with no new measurement. An undo nobody asks for in time changes nothing |

Implementation clarification from the approved S5 plan: D5 ships counts of stored armed, used
and unavailable records, not a duration metric or a live poll of eligibility. It does not measure
how often a wrong rating is caught in time. See the S5 build record for the exact metric semantics.

## 4. What changes, if approved

- **Mock CLM:** the contract record keeps `risk_level` and `risk_summary`. Version 3.0.0 of the
  write sets them and moves the revision in its guarded transaction. A restore write, conditional
  on a given revision, keyed and read back by key.
- **Contract review app:** `crm.write_assessment@3.0.0` (preparation adds `prior`), the
  `crm.restore_assessment@1.0.0` implementation (a trusted-only manifest excluded from model plans), its preparation, read-back,
  guarantees (`conditional_write`, and a new `reversible` that only a connector declares, each with
  a conformance check), credential scope (`assessment:restore`) and rules.
- **Engine:** an `undo_records` store (migration 0010) written with the read-back intent and
  armed by read-back agreement; the `undo` profile and its single prepared step; the undo
  endpoint; a post-terminal link event. These are engine changes, listed here for that reason.
- **UI:** an Undo button on an undoable step, the undo card (prior and current rating, the
  revision check, later steps that used the output), and the restore's approval card.
- **Tests:**
  - change away and back is refused;
  - an unknown or mismatched original has no undo;
  - two undo requests make one change;
  - racing an undo against another write lets one win;
  - restore to "low" needs legal;
  - undo of an email, e-sign, replay or grant is `not_reversible`;
  - undo of an undo is refused;
  - another tenant's step is not found;
  - a document asking for undo changes nothing (the Planner can't reach it);
  - the lost update in section 2 is now `record_changed`;
  - the adversarial oracle and boundary registry.
- **Docs:** threat model (a new row, and a line in T31 that a real CRM must move the revision on
  its own write), API docs, architecture, roadmap, completion records. Credit the readers.

## 5. Expected changes to existing numbers

- **Scripted benchmark:** none expected. Undo can't be planned, and the scripted runs don't write
  twice to one contract. If a case does, it shows as `record_changed` and is reported, not
  hidden.
- **Integrity scenarios:** the same, plus new undo scenarios reported as their own group.
- **Behaviour change:** two runs bound to the same contract revision no longer both write; the
  second fails `record_changed` and needs a new run.

## 6. Limits

- Mocks only. A real CRM must move the record's revision on every write, including GAWE's, and
  accept a write only while the revision matches. Without that, the connector can't declare
  `reversible` and undo is refused.
- Undo restores the rating; it doesn't recall anything done with it: an email sent with the
  rating, a decision someone made after reading it.
- A person must ask. Undo nobody asks for changes nothing, and nothing measures how often a wrong
  rating gets caught in time.
- One action type. Grants ("end now") are B in D1, not undo.

## 7. Approval

Approved by the owner on 2026-11-24: D1–D5 as drafted, each with the recommended option, the
name M26, and no paid runs. The build plan is in `docs/m26-plan.md`, approved the same day.

## Build record

### S1. A current rating in the contract system

The contract record now holds its current rating (`risk_level`, `risk_summary`), absent in the
seed. `MockCLM.set_assessment` (for 3.0.0) checks the bound revision, sets the rating and issues a
new revision in one transaction, and records the operation with the revision before and after
(`result_version`). `restore_assessment` puts back a given rating, or removes it, only while the
contract has a given revision, under its own key and its own scope (`assessment:restore`).
`get_contract` leaves the rating out, so what tools read about a contract is unchanged.
`current_assessment` returns the rating and the revision from one read, for trusted preparation.
`MockOperations.accept` gained an `effect`, run after the guard in the same transaction. The
2.0.0 write still only appends.

Settled here: startup refuses guarantee declarations for tool versions the app doesn't register,
so the declarations for 3.0.0 (with `reversible`) move to S2 and the restore's to S3, each with
its conformance check. S1's tests are at the connector level (`tests/unit/test_current_rating.py`,
11 tests): the write moves the revision; a retry changes nothing; the lost update and a race on one
revision, where one lands and one gets `record_changed`; restore with a new revision, to no rating,
once per key; a restore after a change away and back is refused; restoring needs its own scope.

Verification: `make check` 3,163 passed, 84 skipped; `make check-docs` 0 problems; scripted 38
records identical to the last run on `main`; integrity 23 of 23.

### S2. The record written before the change

`crm.write_assessment@3.0.0` is the current version; 2.0.0 is retired and stays registered (with
its preparer) to verify earlier writes. `AssessmentPreparer` reads the current rating and the
revision in one read and adds `record.contract_version` and a trusted `prior` (`risk_level`, with
`none` for no rating, and `summary`) inside the hash. `prior.risk_level` is typed and
`prior.summary` free text, so the approval card shows the rating being replaced next to the new
one.

`gawe.core.undo` holds the record, its store protocol, an in-memory store and `settled`.
`VerificationResolver.prepare` adds the record before the read-back intent, as it does the expiry
job. Read-back results gained `facts`, values the target reports that aren't compared with the
action; the 3.0.0 adapter reports `result_version`, and the resolver keeps facts in the job's
evidence (only when there are any, so other writes' evidence is unchanged). The record is settled
in `_apply`, from the stored result and evidence, so recovery after any crash settles it once:
armed on agreement with the revision after the write, otherwise unavailable with the reason. The
Postgres store is `gawe.db.undo_store` over migration 0010 (`undo_records`, with checks that armed
means a result revision and unavailable means a reason, and a grant without delete).

`AppBundle.undoable` declares contract review's 3.0.0 write. Startup refuses an entry that isn't
a current write or whose target doesn't declare the new guarantee `reversible`, which has its
conformance check (write, read back the revision, restore at it, and a second restore refused).

Settled here:

- **A prepare-only undoable write starts, with no undo record**, instead of refusing to start:
  GAWE never sends it, so there's nothing to undo. `build_engine` leaves it out.
- **Tests that named 2.0.0 as the current rating write** now name 3.0.0, and the test helper that
  fills trusted fields adds a default `prior`. The pre-M16b bundle in the record-version tests
  drops every later version. The conformance target binds the revision the flow's own write
  left, since that write now moves it.

Tests: `tests/unit/test_undo_record.py` (14), `tests/contract/test_undo_store.py` (4, on both
backends), `tests/sql/test_undo_records.sql` (6 checks), and the `reversible` and 3.0.0
conformance cases.

Verification: `make check` 3,191 passed, 88 skipped (the four new Postgres store tests skip
without a database); `make test-integration` 134 passed; `make test-schema` 114 checks;
`make check-docs` 0 problems; scripted 38 records identical to the last run on `main`; integrity
23 of 23; replay 9 of 9.

Noted for S3: M24's rating history counts `crm.write_assessment` steps, so after an undo it would
still treat the undone rating as the standing one. S3 must handle it.


### S3. The reversal — incomplete checkpoint (2026-11-25)

The owner approved option A for M24 history: count restore actions with rating writes, including
shared history and subject locking across both tools. A successful restore to no rating clears
the standing rating; pending and unknown ratings retain the existing conservative treatment.

Partial implementation only: manifests have `trusted_only`; the Planner omits them and plan
validation refuses them using the existing `UNKNOWN_TOOL` issue code. History subjects can
explicitly declare a tool family, read its combined entries, retain the actual tool name on
cards, and share one subject lock. Startup checks family membership and subject consistency.
Tests cover the boundary and shared history/locking. The contract app is not yet wired to a
family: the restore manifest, implementation, endpoint, recovery, link events, policy changes,
used-record states, SQL changes, and undo view remain unbuilt.

Stopped under the owner's two-verification-failures rule:

- `bench-results/m26-s3-foundations.txt`: lint passed; mypy failed on a reused local variable in
  plan validation. Fixed by giving the registry lookup its own local name.
- `bench-results/m26-s3-foundations-retry.txt`: lint and mypy passed; the focused pytest run
  failed in the new trusted-only test fixture because it omits mandatory field classification.
  The other focused tests passed. The failing fixture has deliberately not been fixed after
  this second failure. No full checkpoint checks or benchmarks were run.

Pending owner decision: the specified refusal order masks `undo_of_undo` behind
`not_reversible`, and used records behind `undo_unavailable`. Proposed clarification is to
exclude those later categories from the earlier predicates, retaining the stated check order
while making every specified code reachable. Asked; no answer received at this checkpoint.

Nothing staged, committed, or pushed. No paid model calls or real connectors used.


#### S3 continuation — incomplete checkpoint (2026-11-25)

The owner committed the foundation, then authorized resuming S3 and approved the
refusal clarification: earlier checks exclude later categories so `already_undone` and
`undo_of_undo` remain reachable, in the specified check order. History option A remains approved.

Implemented in this uncommitted continuation:

- Fixed the trusted-only test fixture's missing field classification.
- Added `crm.restore_assessment@1.0.0`, its restore-only ScopedClient, preparation, read-back by
  its own key (prior text digested in evidence), and three guarantee conformance cases.
- Added POST and GET `/v1/runs/{run_id}/steps/{step_id}/undo`. The view lists later direct `$ref`
  consumers and says their effects stay. The POST checks tenant/step, case-manager role and the
  specified refusal categories. Public run creation refuses the `undo` profile.
- An `undo_request` maintenance job records the request before run creation. A shared lock
  serializes requests and recovery. Recovery derives one run id and operation key from the
  original key (`gawe.undo.v1`), constructs the plan from the captured record, and supplies it
  before execution; an undo run without its plan never calls the Planner. This reuses the
  existing maintenance runner for the required request-to-run crash window.
- Added `requested`, `used`, and `failed` undo states and extended migration 0010's constraints.
  Independent read-back agreement marks the record used. Original-run link events include both
  actions' hashes; the undo run retains its own chain. The request job retries bookkeeping.
- Wired writes and restores into shared rating history/locking. Common rating rules now select
  both tools; restoring no rating has an explicit legal-review rule. Added tests for automatic
  restore, low/absent approval, away-and-back refusal, tenant/role/profile boundaries, duplicate
  requests, and a crash before run creation.

Verification stopped after the second failed attempt, per the owner's rule:

1. `bench-results/m26-s3-resumed-check-1.txt`: lint passed; mypy rejected the inferred fixed-length
   MANIFESTS tuple after appending the restore. Fixed with an explicit variable-length tuple type.
2. `bench-results/m26-s3-resumed-check-2.txt`: lint and mypy passed; pytest **3,202 passed,
   88 skipped, 4 failed**, in 342.72 seconds. The new undo-flow and restore-conformance tests
   passed. Failures, left unfixed after the stop:
   - `tests/adversarial/test_policy_boundaries.py::test_every_shipped_rule_has_an_independent_boundary`:
     register an independent boundary for `remove-rating-needs-legal`.
   - `tests/adversarial/test_policy_unreadable.py::test_property_targets_cover_all_shipped_tools_paths_and_variables`:
     add restore targets; do not exclude the trusted tool from policy coverage.
   - `tests/unit/test_controls.py::test_no_api_route_changes_a_control`: review/register the two
     undo routes in the explicit route allowlist.
   - `tests/unit/test_policy_provenance.py::test_safe_callbacks_remain_unchanged_after_real_mock_writes`:
     add a real restore action to the contract-review fixture.

Remaining before S3 can be approved: fix the four failures, complete tests for the remaining
refusals, HTTP behavior, downstream view, post-undo history (including no rating and pending
ratings), write/undo races, dispatch/read-back crash recovery, and audit-chain verification.
Review cancellation before materialization and crash recovery between run insertion and audit
creation; these windows are not covered yet. Map unreadable undo-store failures to the intended
refusal. Validate the new states on Postgres and add SQL coverage. Editing migration 0010 does
not update a database where 0010 was already applied; an existing-database upgrade path remains
unverified. No full-check success is claimed.

Not run after the mandated stop: `make check-docs`, scripted comparison against
`bench-results/20261125T041836Z-scripted`, integrity, replay, Postgres integration and schema
checks. No paid calls, real connectors, staging, commits or pushes. S4 has not started.


#### S3 completion continuation — verification stop (2026-11-25)

The owner committed the prior continuation and explicitly requested continuing S3.
The four failures from that checkpoint are fixed: the removal rule has an independent boundary
oracle; restore fields and policy variables have unreadable-input targets; the two undo routes
are reviewed in the control-route allowlist; and the provenance test performs a real restore.

Additional changes: unreadable undo storage refuses with `undo_unavailable`; recovery repairs
`run.created` after a crash following insertion; cancellation before materialization creates the
undo step records before terminating them. Failure-link retries retain the stored reason. New
migration 0011 applies the reversal-state constraints to already-existing S2 databases, as well
as the expanded fresh-install 0010. Its downgrade keeps the constraints accepted by current 0010.

New tests cover non-reversible tool categories, unconfirmed/unreadable originals, high→low then
undo without the Planner, independent audit-chain verification, later ratings after restoration
to high/low/none, pending history after removal, request/run-insert/dispatch/read-back crash
windows, cancellation, HTTP/view behavior, and a write racing the restore. A Postgres end-to-end
test exercises the assembled API, run creation, history locks, read-back, record states and both
audit chains. Store contracts cover requested/used/failed on both backends.

Verification:

- `bench-results/m26-s3-completion-focused-1.txt`: lint/types passed; focused tests found
  cancellation before the trusted plan was materialized raised `StepNotFoundError`. Fixed in
  the undo-profile cancellation path only.
- `bench-results/m26-s3-completion-focused-2.txt`: lint/types and the focused retry passed.
- `bench-results/m26-s3-completion-check.txt`: **make check passed**, **3,265 passed,
  92 skipped**, 339.62 seconds; lint and mypy clean.
- `bench-results/m26-s3-completion-integration.txt`: **141 passed**, including durable undo.
- `bench-results/m26-s3-completion-schema.txt`: **failed after 114 checks**. The new SQL upgrade
  test uses `\i migrations/sql/0011_undo_reversal_states.sql`. The harness feeds SQL on stdin to
  psql inside Docker, whose filesystem does not mount the repository; that include cannot read
  the host file. The harness filters out the include diagnostic, retaining only PASS/FAIL/ERROR
  lines. Migration 0011 itself applied successfully, both here and in integration fixtures.

The schema failure is the second verification failure in this continuation, so changes stopped
under the owner's rule. The already-running full check was allowed to finish and passed. Next:
make the SQL upgrade test self-contained for the Docker/stdin runner, rerun `make test-schema`,
then `make check-docs`, scripted (field-by-field against 20261125T041836Z-scripted), integrity,
and replay. Do not claim existing-S2 upgrade verification until the repaired SQL test passes.
No benchmark result is claimed yet. S3 is still incomplete; S4 has not started. Changes remain
unstaged and uncommitted. No paid calls or real connectors.


### S3. Completed checkpoint (2026-11-25)

The owner committed the prior work and authorized finishing S3. The remaining schema
failure is fixed in `scripts/test_schema.sh`: repository-local `\i` includes are expanded on the
host before SQL is streamed into Docker's psql. The upgrade test therefore executes the real
migration 0011 without copying its implementation or requiring a repository mount. Failed SQL
tests now retain their complete diagnostics instead of filtering away include errors.

S3 is complete and stops here for owner review. The cumulative implementation is described in
the continuation records above: trusted-only restore, captured-state preparation and conditional
write/read-back, authorized undo API and dependent-step view, deterministic reversal identity,
durable request recovery, legal-review rules, shared rating history/locks, independent audit
chains with original-run links, and persisted reversal states. The approved refusal clarification
and history option A are implemented. S4 UI work has not started.

Final verification evidence (all logs ignored under `bench-results/m26-s3-*.txt`):

| Check | Result | Log |
|---|---|---|
| `make check` | Lint and mypy clean; 3,265 passed, 92 skipped | `m26-s3-completion-check.txt` |
| `make test-integration` | 141 passed, including durable undo | `m26-s3-completion-integration.txt` |
| `make test-schema` | 118 checks passed, including S2 in-place upgrade and reversal states | `m26-s3-final-schema.txt` |
| `make check-docs` | 259 references, 0 problems | `m26-s3-final-check-docs.txt` |
| `python -m bench scripted` | 38 records | `m26-s3-final-scripted.txt` |
| Exact scripted comparison | 38/38 identical to `20261125T041836Z-scripted`, every field/type/key/list order, nothing excluded | `m26-s3-final-scripted-comparison.txt` |
| `python -m bench integrity` | 23/23 expected records | `m26-s3-final-integrity.txt` |
| `python -m bench replay` | 9/9 expected records | `m26-s3-final-replay.txt` |

The Python full check and Postgres integration results are retained from the preceding
continuation: Python production code, migrations and Python tests are unchanged since those
passing runs. This continuation changes only the SQL runner and checkpoint documentation; its
applicable schema check was rerun and passed. Benchmark artifacts are
`20261125T193211Z-scripted`, `20261125T193211Z-integrity`, and `20261125T193211Z-replay`.

Known limits remain the approved ones: mock rating restoration only, requested by a person;
other effects are not recalled, later revisions block restoration, and unknown original outcomes
cannot be undone. The existing integrity content gap B02 remains an expected demonstrated gap,
not a regression or a claim of content safety. The UI is S4; additional undo benchmark scenarios
and telemetry are S5. Migration 0011 still must be applied when upgrading a deployed S2 database;
verification used isolated test databases and did not migrate the owner's application database.
No paid model calls, real connectors, staging, commits or pushes.

Suggested commit for this final continuation:
`fix(test): stream SQL migration includes and complete M26 S3 verification`


### S4. Undo from the run page — completed checkpoint (2026-11-25)

The owner committed S3 and authorized S4. The run page now loads the existing undo
view for rating/restore steps and shows an Undo button only for a case manager with an armed,
unused record matching the displayed step/action hash and carrying its result revision. Other
states explain why undo is unavailable; restores cannot themselves be undone. A failed status
read exposes no request button. A refresh control works even when the original run is terminal.

The expanded undo card shows the rating written by the step (from its verification evidence),
the previous rating to restore, the conditional revision check in words, and later steps that
referenced the output with their tool and outcome. It explicitly says those effects stay.
Restoring low or no rating says legal review is required. The previous summary is labelled as
model-written text and collapsed. The existing approval card keeps the restore's prior rating
in its typed fields and the summary separate; it adds a reminder about the lasting effects.

Settled presentation details: these are captured ratings, not a live CRM read, and the card says
so. "Undo succeeds only if the contract hasn't changed since this rating was written" describes
the write-time condition rather than claiming the UI has checked it already. The request goes
through a server action using the httpOnly-cookie session and submits only run/step ids; no
rating, prior text, revision, or policy setting is accepted from the form. The action checks the
role for presentation and the API remains authoritative. On success the page opens the separate
undo run, including its normal approval-waiting state. Duplicate requests, changed records and
uncertain request outcomes have plain explanations; submission is disabled while pending.

Files: typed undo API methods/types, `ui/lib/undo-view.ts` for the pure card model,
`ui/app/runs/[id]/UndoCard.tsx`, the run page/server action and existing approval card, plus
card-model/client/approval tests. No backend, policy, database or engine changes.

Verification (all logs ignored under `bench-results/m26-s4-*.txt`):

| Check | Result | Log |
|---|---|---|
| `make ui-check` | TypeScript clean; 60 tests passed; production build passed | `m26-s4-ui-check-1.txt` |
| `make check` | Lint/mypy clean; 3,265 passed, 92 skipped, 329.44 seconds | `m26-s4-check.txt` |
| `make check-docs` | 259 references, 0 problems | `m26-s4-check-docs.txt` |
| Scripted benchmark | 38 records; every field/type/key/list order identical to `20261125T041836Z-scripted` | `m26-s4-scripted.txt`, `m26-s4-scripted-comparison.txt` |
| Integrity benchmark | 23/23 match expected records | `m26-s4-integrity.txt` |

Benchmark directories: `20261125T201949Z-scripted` and `20261125T201949Z-integrity`.
Tests cover role/state/hash gating, missing records/evidence, no-rating removal, approval notes,
restore/non-reversible refusals, later-effect warnings, text separation, URL encoding, empty
request bodies and API refusal codes. No verification attempts failed.

Known limits: the card shows captured values; later changes are refused by the existing atomic
revision check. The existing B02 content gap remains in the integrity benchmark. Per the plan,
verification uses card models rather than a rendered-page test; no optional paid/manual UI pass
was performed. Postgres integration/schema checks were not rerun because S4 changes no code they
exercise. No paid model calls or real connectors. No staging, commits or pushes. S4 is complete
and stops for owner review; S5 has not started.

Suggested commit: `feat(ui): undo a rating from the run page`

### S5. Adversarial coverage, undo integrity, and counts — completed checkpoint (2026-11-25)

The owner committed S4 and authorized S5. Restore boundary coverage now crosses
none/low/medium/high with the prior rating and the unsupported-level/large-deal variables, using
hand-written expected outcomes and rule ids. The existing boundary registry includes removal.
Unreadable restore inputs remain in the general adversarial targets; new cases cover nested
prior fields, malformed current ratings, connector read errors, and unreadable armed records.
Preparation now distinguishes no rating (`None`) from a malformed/empty rating object and
validates the trusted prior, returning `record_version_unavailable` before policy or dispatch.
An undo request checks the assembled restore arguments against the restore manifest before
writing its request job; malformed prior fields or overlapping subject/prior keys return
`undo_unavailable` without starting a run. No policy rule or approval requirement changed.

`bench/integrity/undo.py` adds its own four-record group and artifact directory, invoked by
`python -m bench integrity` alongside the unchanged 23-case group. It covers a real change away
and back, an accepted original write whose independent read-back remains invisible through its
deadline, simultaneous duplicate undo requests, and an undo racing another actor's guarded
write. Each compares an independent expected record including acceptance at the target, errors,
new-run count, final rating, undo state and audit validity. Only the race winner's identity is
normalized: exactly one acceptance, `record_changed` for the loser, and consistent final fields
and state are still checked. Original writes use a fake Planner; undo uses its trusted plan.

D5 telemetry uses `gawe.undo.records`, a count snapshot observed on each maintenance tick,
labelled only by tenant, state and a bounded reason code. Both stores aggregate by tenant,
state and reason; Postgres selects no subject or prior content. Armed and used counts have no
reason; unavailable includes failed reversals, with unrecognized codes grouped under `other`.
Pending/requested records are in flight and omitted. Empty buckets emit zero, and repeated ticks
observe the same count rather than incrementing it. A failed count read reports a maintenance
error, not invented zeroes. This reuses the existing histogram/observation interface used for
maintenance backlog; consumers must treat the observations as snapshots, not sum them as events.

Verification logs are ignored under `bench-results/m26-s5-*.txt`:

| Check | Result | Log |
|---|---|---|
| Focused adversarial/undo/store/benchmark tests | 739 passed, 8 skipped | `m26-s5-focused-1.txt` |
| `make check` | Lint/mypy clean; 3,347 passed, 93 skipped, 353.09 seconds | `m26-s5-check.txt` |
| `make check-docs` | 259 references, 0 problems | `m26-s5-check-docs.txt` |
| Postgres integration | 143 passed | `m26-s5-integration.txt` |
| Scripted benchmark | 38/38 identical to `20261125T041836Z-scripted` | `m26-s5-scripted.txt`, `m26-s5-baseline-comparison.txt` |
| Existing integrity group | 23/23; identical to `20261125T201949Z-integrity` | `m26-s5-integrity.txt`, `m26-s5-baseline-comparison.txt` |
| Undo integrity group | 4/4 match independent records | `m26-s5-integrity.txt` |

Both baseline comparisons include every field, scalar type, key set and array order, with no
fields ignored. Artifacts: `20261125T203710Z-scripted`, `20261125T203710Z-integrity`, and
`20261125T203710Z-undo-integrity`. One verification attempt failed: mypy caught the race hook
returning the barrier's integer rather than `None`. That hook was corrected; the second mypy
check passed for all 158 source files (`m26-s5-type-1.txt`, `m26-s5-type-2.txt`).

Known limits: counts describe stored undo states, not live proof that the contract revision is
still unchanged. A later external change is detected when restoration attempts its atomic guard;
this step adds no CRM polling or duration metric. Undo remains person-requested and mock rating
restoration only. The original B02 content gap remains demonstrated. No schema migration or UI
change was needed; their dedicated checks are not rerun in S5. No paid model calls, real
connectors, staging, commits or pushes. S5 is complete and stops for owner review; S6 has not
started. S6's broader documentation pass remains separate.

Suggested commit: `test(undo): adversarial and integrity cases for undo`


### S6. Final verification and documentation — completed checkpoint (2026-11-25)

The owner committed S5 and authorized S6. This checkpoint changes documentation
only. The API reference documents GET/POST undo, tenant/role boundaries, captured view fields,
request refusal codes, and the distinction between an endpoint refusal and `record_changed` on
the restore step. The architecture covers prior-state capture, trusted-only plans, durable
request recovery, shared write/restore history, scoped restoration and counts-only telemetry.
The threat model adds T46 with executable proof references; T31 now states that a real CRM must
move its revision on GAWE's own writes as well as other relevant mutations. T11 acknowledges
captured prior text in the tenant-scoped undo view, and T43 includes the shared rating family.

The demo has UI and HTTP undo walkthroughs with approval of the specific reversal run, expected
failures, both audit chains and a free benchmark alternative. The roadmap records the M26 scope
and credits the r/LLMDevs readers' 2026-11-13 suggestions on prior-state capture and conditional
restoration. Earlier design wording is clarified against the approved build plan: the restore
has a trusted-only manifest; D5 implements state counts, not undo-lifetime measurements.

Final checkpoint logs are ignored under `bench-results/m26-s6-*.txt`:

| Check | Result | Log |
|---|---|---|
| `make check` | Lint/mypy clean; 3,347 passed, 93 skipped, 354.86 seconds | `m26-s6-check.txt` |
| `make test-integration` | 143 passed, isolated Postgres schemas | `m26-s6-integration.txt` |
| `make test-schema` | 118 checks passed, including the S2 upgrade path | `m26-s6-schema.txt` |
| `make ui-check` | TypeScript clean; 60 tests passed; production build passed | `m26-s6-ui-check.txt` |
| `make check-docs` | 275 references, 0 problems | `m26-s6-check-docs.txt` |
| New demo shell blocks | 2 pass `bash -n`; not executed | `m26-s6-demo-syntax.txt` |
| Scripted benchmark | 38 records, identical to `20261125T041836Z-scripted` | `m26-s6-scripted.txt` |
| Existing integrity group | 23/23, identical to `20261125T201949Z-integrity` | `m26-s6-integrity.txt` |
| Undo integrity group | 4/4, identical to S5's `20261125T203710Z-undo-integrity` | `m26-s6-integrity.txt` |
| Replay benchmark | 9/9 match expected records | `m26-s6-replay.txt` |

`m26-s6-baseline-comparison.txt` records strict comparisons of every field, scalar type, key set
and array order, with nothing ignored. New artifacts are `20261125T204921Z-scripted`,
`20261125T204920Z-integrity`, `20261125T204920Z-undo-integrity`, and `20261125T204921Z-replay`.

Known limits are unchanged: only mock current-rating/summary restoration, requested by a person;
no recall of later effects, and no undo of an unknown original or an old assessment version.
External edits are detected by the atomic guard at restoration, not by a live eligibility poll.
Telemetry is a snapshot of stored states; it is not evidence for dropping legal review or a
measurement of how often wrong ratings are caught in time. The original B02 encoded-content gap
remains a demonstrated gap. UI verification covers card models and the production build, not a
rendered-page test. The optional paid/manual UI pass was not performed.

Schema tests used a throwaway database and integration tests used isolated schemas; this step
did not migrate the owner's configured application database. Deployments with the original S2
schema must apply migration 0011 before using reversal runs. No paid model calls, real connectors,
public port, staging, commits or pushes. No verification attempts failed in S6.

S1–S6 are complete. S6 is uncommitted and stops for owner review; the optional paid UI pass is
not required by the approved plan. The next milestone requires a separate owner choice.

Suggested commit: `docs: record verified M26 results`
