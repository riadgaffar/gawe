# M27 — Explain a refused record change

Status: **Done.** Design and build plan approved by the owner; S1–S6 committed by the owner and merged to `main` by fast-forward (2026-11-26).
Base: M26. Branch: `record-change-diff`.

This is roadmap item 11. When a revision-bound action is refused, explain which policy-relevant
record fields differ from the values captured for approval. The revision check still refuses
the action even when those selected fields did not change. A fresh run is still required.

## Approved design

### D1 — Capture declared policy fields with the revision

Each participating app declares a bounded set of typed record fields. Trusted preparation
captures those fields and the guarded revision from the same record read, inside the resolved
arguments covered by the action hash. The approval card shows those captured values. No whole
record, arbitrary free text, or target history is needed. Missing and null values are distinct.

Resource-derived policy variables use the same captured values. Contract deal value currently
has a separate read; remove that split when activating snapshot preparation. Ticket, tenant
configuration, and history inputs remain their own policy inputs. IAM's account guard does not
mean its policy reads account profile fields: explicitly declare no such fields tracked.

### D2 — Store honest evidence of the refusal

Cover the existing revision-bound writes, including trusted undo and replay. Persist observations
for `record_changed` and replay's `event_changed` so refresh and recovery show stable evidence.
An observation identifies the resource, subject, revision when available, and observation time.
Evidence returned by the atomic guard is marked `at_guard`; a diagnostic read after refusal is
marked `after_refusal`. A later read must never be described as the historical guard state.

The diagnostic binds to the refused action hash and its authorized, tenant-scoped targets.
Multi-target evidence records complete or partial coverage. An unreadable or missing record is
explicit, never an invented empty comparison. Diagnostic work cannot retry the write, authorize
an effect, or change the refused outcome.

### D3 — Compare and present without weakening the guard

Show captured and observed values with the timing of the observation. Distinguish changed,
unchanged, no fields tracked, snapshot unavailable, observation unavailable, and record missing.
When all tracked fields match, explain that the revision changed but those fields did not.
Continue to refuse; do not request a second approval on the same action.

### D4 — Preserve existing durable identities

Old steps and approvals keep their original arguments, hashes, tool versions, and behavior.
Without an original snapshot, show snapshot unavailable rather than backfilling current values.
Introduce new tool versions when snapshot schemas become active; retain the compatibility
paths needed for already persisted work. Trusted undo requests persist their chosen restore
version. Unversioned M26 requests/plans mean `crm.restore_assessment@1.0.0`, never latest.
Model-produced plans cannot select internal versions or trusted-only tools.

### D5 — Undo provenance and daily review

Undo may carry trustworthy policy-field evidence captured for the original confirmed action,
identified as such and bound to its source action hash. Do not relabel a fresh read as that
original evidence. Snapshotless old undo records retain their existing behavior. The daily review
continues its separate evaluation against current state; historical snapshots are not a reason
to freeze that review's policy inputs.

### D6 — Scope and verification

Use existing JSON persistence for snapshots and diagnostics; no new table is planned. Keep
policy rules, revision refusal semantics, retry behavior, and approval requirements unchanged.
No paid model calls or real connectors. Rendering tests for the approval card belong to item 12;
this milestone tests API/card models and existing UI checks.

Build and review one step at a time using [the approved plan](m27-plan.md). The owner commits.
Stop after two failed verification attempts in a step and report before continuing.

## Build record

### S1 — Snapshot and diagnostic contracts

Added `core.record_changes` contracts for declared scalar fields, captured snapshots, and
refusal observations. Values distinguish absent, null, and unavailable. Declarations reject
missing/extra paths and incorrect scalar kinds; models reject unknown members, duplicate fields
or subjects, unbounded text/collections, and non-finite numbers. Diagnostics bind to an action
and its target set, retain guard/later-read timing, and support complete/partial coverage. The
200-observation limit accommodates the existing replay schema's full batch bound. Comparison
returns explicit presentation states without making an authorization decision.

Snapshots serialize into existing resolved-argument JSON; diagnostics fit existing maintenance
result JSON. Shared memory/Postgres contract tests verify both round-trips, including observation
timing and action binding. No migration, table, connector, active tool schema, or policy rule
changed in S1. Hash tests show that changing captured values, subject, revision, or field coverage
changes the prepared action's hash.

Trusted undo plans now have an optional persisted `tool_version`, and new durable undo requests
save the selected version before run creation. Recovery, materialization, pre-materialization
cancellation, and operation-key resolution use that version. Unversioned M26 restore requests
and plans retain their fixed 1.0.0 meaning. Compatibility normalization is only for comparison;
old plans, arguments, plan versions, approval hashes, and audit records are not rewritten.
Unknown legacy tools or unavailable pinned versions fail closed instead of choosing latest.
The model's output schema excludes pins, and validation rejects even explicitly null model pins.
An absent pin is omitted during serialization, preserving old plan/audit JSON shapes.

Tests exercise old requests before run creation, old plans before materialization, and approval-
waiting restores across registration of a newer manifest. They verify the original action hash,
version, reversal key, and one target effect; a mismatched version is refused. New requests retain
their persisted selected version across another registry upgrade.

Verification completed with zero failed attempts. Logs are ignored under `bench-results/`:

| Check | Result | Log |
|---|---|---|
| `make check` | Lint clean; mypy 159 files; 3,380 passed, 96 skipped, 352.93 seconds | `m27-s1-check.txt` |
| Focused contracts, compatibility and store tests | 79 passed, 24 Postgres cases skipped here | `m27-s1-focused-1.txt` |
| Final 200-event bound and snapshot/diagnostic tests | 23 passed; includes the additional boundary test added during the full run | `m27-s1-batch-bound.txt` |
| Final lint after that boundary adjustment | Clean | `m27-s1-final-lint.txt` |
| `make test-integration` | 149 passed in isolated Postgres schemas | `m27-s1-integration.txt` |
| `make check-docs` | 275 references, 0 problems | `m27-s1-check-docs.txt` |
| Scripted benchmark | 38 records, identical to M26 | `m27-s1-scripted.txt` |
| Integrity benchmark | 23/23 expected; identical to M26 | `m27-s1-integrity.txt` |
| Undo integrity group | 4/4 expected; identical to M26 | `m27-s1-integrity.txt` |
| Replay benchmark | 9/9 expected; identical to M26 | `m27-s1-replay.txt` |

`m27-s1-baseline-comparison.txt` records strict comparisons of every field, scalar type, key set,
and array order, with nothing excluded. New artifacts: `20261125T211916Z-scripted`,
`20261125T211918Z-integrity`, `20261125T211918Z-undo-integrity`, and `20261125T211919Z-replay`.
No SQL schema or UI files changed, so their dedicated checks were not needed for S1.

Known limits: S1 supplies the contracts and version compatibility foundation only. Shipped tools
do not yet capture these snapshots or produce diagnostics, and the UI has no new comparison
view. A source action hash in the model records provenance but does not by itself authenticate
connector evidence; later trusted adapters must establish that provenance. Snapshot activation,
refusal persistence, undo propagation, and UI wiring remain S2–S5. The existing B02 encoded-content
gap is unchanged and still demonstrated by the integrity benchmark.

Stop here for owner review/commit. No S2 implementation, paid calls, real connectors, staging,
commits, or pushes.


### S2 — Capture and same-read policy values

Started from owner-committed S1, with explicit authorization to implement S2. The
initial implementation stopped after two failed verification attempts, as required by the plan.
The owner committed that checkpoint and authorized the continuation recorded below.

Implementation currently adds snapshot execution versions: assessment 4.0.0, signature 3.0.0,
IAM grant 3.0.0, and replay 2.0.0. Preceding active versions remain registered and executable for
persisted steps and approvals. Their schemas are unchanged. All new snapshots are in the hashed
arguments, excluded from model proposal schemas. Contract snapshots track deal value; replay
snapshots track customer and amount from each existing frozen event read; IAM declares no account
fields tracked. Whole records, account profiles and event payloads are not stored in snapshots.

A trusted contract read returns the record and revision together. Assessment preparation takes
its prior rating and deal value from that read. A version-specific action-variable callback uses
the captured deal value for `review.large_deals`, without the old second resource read. The base
variable callback remains available to old versions and daily review, preserving its current-
state semantics. Proposed signature amounts are unchanged and retain the existing target-side
`deal_value_mismatch` check. Existing revision guards remain unchanged. Read-back, expiry,
dispatch conditions, undo eligibility and connector guarantee declarations include new versions.
Undo snapshot propagation remains S4; restore 1.0.0 has not changed.

The new S2 tests cover same-read capture and prior state, model-output poisoning, malformed
values, rejected model snapshot input, action-hash binding, race refusal, old materialized steps
and approvals across restart, replay capture, and separate current-state variables. These passed
within the focused run. Other tests/fixtures were updated for multiple active versions.

Initial verification before the stop:

| Attempt/check | Result | Log |
|---|---|---|
| Initial formatting/lint | Clean after routine import fixes | `m27-s2-lint-1.txt` |
| Failure 1: mypy | Two fixed-length manifest tuple annotation errors | `m27-s2-type-1.txt` |
| Mypy after annotation fix | Clean, 159 source files | `m27-s2-type-2.txt` |
| Failure 2: focused suite | 457 passed, 5 failed, 19.74 seconds | `m27-s2-focused-1.txt` |

The five failures at that stop (resolved in the continuation below):

- `test_record_versions.py::test_an_unexecuted_v1_step_fails_closed_after_the_upgrade[crm]`
  and `::test_a_dispatched_v1_write_still_verifies_after_the_upgrade[crm]`: `_legacy_bundle`
  removes hard-coded versions 2 and 3, leaving new version 4 in its simulated pre-M16b registry.
  The supposed v1 run therefore executes version 4. Rebuild the fixture using all later versions
  and filter its associated maps (including action variables) consistently; retain both checks.
- `test_guarantees.py::test_a_write_judged_by_a_running_total_needs_atomic_reservation`, direct
  and through-a-variable variants: the synthetic counter policy applies to both active grant
  versions, but the fixture adds reservation or prepare-only status only to version 2. The
  startup guard correctly rejects version 3 without its required guarantee. Adjust the fixture
  for all affected versions; do not claim a new real IAM guarantee.
- `test_undo_unreadable.py::test_unreadable_current_rating_fails_before_policy[high]`: the new
  test translation maps a malformed old accessor result `"high"` into a valid record containing
  `risk_level="high", risk_summary=""`. It reaches the write and rejects the fake revision.
  Correct the malformed-record fixture while preserving old-accessor coverage.

Not run after the mandated stop: full `make check`, `make check-docs`, Postgres integration,
scripted/integrity/undo/replay benchmarks and their exact baseline comparison. No SQL/UI files
changed. No staging, commits, pushes, paid calls or real connector use occurred.

#### Authorized continuation from the stopped S2 checkpoint

The five failures were fixture defects introduced by the version transition. The legacy CRM
fixture now removes every later version, including its action-variable adapter. The synthetic
IAM counter-policy fixture supplies its test-only reservation guarantee (or prepare-only setting)
to every executable grant version, and separately proves that removing it from either version
is refused. No actual IAM connector guarantee changed. Malformed-prior cases preserve malformed
response shapes and now exercise both the old assessment accessor and the new record accessor.

Two additional benchmark-test fixtures now apply prepare-only mode to the current assessment
and signature versions. The benchmark scenarios, expected outcomes and baseline records did not
change. This continuation changes tests and documentation only; production code remains the
implementation in the stopped S2 checkpoint.

The first full continuation run found nine further fixture failures (3,429 passed, 96 skipped):
existing snapshot fixtures were unnecessarily rebuilt from intentionally malformed arguments;
two lease-test hooks targeted assessment 3 rather than the dispatched version; expiry and
provenance fixtures enumerated only pre-M27 versions; and one handoff fixture targeted grant 2.
The helper now preserves a supplied snapshot, lease hooks use the actual step version, and the
remaining fixtures cover the new versions. Provenance rejection now explicitly includes
assessment 4. No safety assertion or expected benchmark outcome was removed.

Final verification passed. This continuation had one failed full-suite attempt, corrected before
the passing full rerun. Logs are ignored under `bench-results/`:

| Check | Result | Log |
|---|---|---|
| First focused set | 112 passed | `m27-s2-resume-focused.txt` |
| Additional affected test groups | 768 passed | `m27-s2-resume-focused-2.txt` |
| First full attempt | 9 fixture failures, 3,429 passed, 96 skipped | `m27-s2-resume-check.txt` |
| Final `make check` | Lint clean; mypy 159 files; 3,439 passed, 96 skipped, 470.77 seconds | `m27-s2-resume-check-2.txt` |
| `make test-integration` | 149 passed, isolated Postgres schemas | `m27-s2-resume-integration.txt` |
| `make check-docs` | 275 references, 0 problems | `m27-s2-resume-check-docs.txt` |
| Scripted benchmark | 38 records, identical to approved baseline | `m27-s2-resume-scripted.txt` |
| Integrity benchmark | 23/23 expected, identical to baseline | `m27-s2-resume-integrity.txt` |
| Undo integrity group | 4/4 expected, identical to baseline | `m27-s2-resume-integrity.txt` |
| Replay benchmark | 9/9 expected, identical to baseline | `m27-s2-resume-replay.txt` |

`m27-s2-resume-baseline-comparison.txt` compares every field, scalar type, key set and array order,
with no exclusions. Artifacts: `20261126T235242Z-scripted`, `20261126T235244Z-integrity`,
`20261126T235244Z-undo-integrity`, and `20261126T235246Z-replay`. Production code and benchmark
inputs were unchanged after the passing database and benchmark checks. No SQL schema or UI files
changed in S2, so their dedicated checks were not required.

S2 is complete. Stop for owner review/commit before S3. Suggested continuation commit:
`test(records): complete S2 compatibility verification`.

S2 scope remains snapshot capture and consistent authorization inputs. Durable refusal evidence,
undo snapshot propagation and the comparison UI remain S3–S5. Old versions preserve their schemas
and hashes; missing historical snapshots are not synthesized. The existing B02 encoded-content
gap remains unchanged. No paid calls, real connectors, staging, commits or pushes.


### S3 — Durable refusal observations

Started from owner-committed S2, with explicit authorization to implement S3.
Revision refusals now persist a bounded proof before closing the attempt or performing any
optional diagnostic read. The proof binds to the existing verification job's action identity.
Once saved, it cannot be removed or changed, and its only terminal outcome is `not_dispatched`
with the original `record_changed` or `event_changed` code. Recovery consumes this proof even
past the normal read-back deadline; it never dispatches the action again.

Mock CLM, IAM and replay guards can attach selected fields from the same atomic read that
rejected the revision. Observations carry `at_guard`, the guarded record's revision and a UTC
observation time. App adapters validate resource, subject, field declarations and timing before
persistence. Foreign, duplicate, malformed or mislabeled observations are discarded. Contract
observations select deal value, IAM selects no account fields, and replay selects customer and
amount. Replay checks tenant ownership before exposing guard fields; its fallback reader also
withholds records that have moved outside the run's tenant.

If guard evidence is absent, the adapter reads only the action's authorized subjects and labels
each observation `after_refusal`. Missing targets, unreadable targets and invalid field shapes
are explicit states; raw records and exception messages are not persisted. A first-member replay
refusal records partial batch coverage, without reading more members to imply a complete guard
observation. Diagnostic adapter failure preserves the proven refusal and marks the diagnostic
unavailable. None of these paths retries a write or requests another approval.

The proof and completed diagnostic use existing maintenance-job JSON storage. The final
diagnostic is immutable and included in the existing audit evidence hash. Refresh and repeated
recovery use saved evidence. A crash after proof persistence resumes diagnostic completion;
a crash after final persistence resumes only the existing continuation. Without saved guard
evidence, a read performed during recovery is honestly marked as a later observation.

Tests exercise CRM, signature, IAM, replay and snapshotless restore refusals, field selection,
action binding, partial batches, missing/unreadable/invalid records, foreign evidence, tenant
changes, broken adapters and crashes before or after diagnostic completion. They assert stable
refusal codes, one write attempt, zero target effects and no diagnostic replacement on refresh.
A Postgres recovery test reconstructs the stores and service after a committed refusal, verifies
the original guard values survive a later record edit, and checks the audit chain.

Final checkpoint verification passed. One initial formatting/lint attempt failed on a long
docstring and a `SIM105` suggestion; both were corrected before subsequent checks. There were
no further failed verification attempts. Logs are ignored under `bench-results/`:

| Check | Result | Log |
|---|---|---|
| Initial lint attempt | Two style issues, corrected | `m27-s3-lint-1.txt` |
| Corrected lint and focused mypy | Clean; mypy 161 source files | `m27-s3-lint-2.txt`, `m27-s3-type-1.txt` |
| Focused tests | 260 passed, 13 database cases skipped here | `m27-s3-focused-1.txt` |
| Final `make check` | Lint clean; mypy 161 files; 3,461 passed, 98 skipped, 469.40 seconds; 92% coverage | `m27-s3-check.txt` |
| `make test-integration` | 152 passed, isolated Postgres schemas | `m27-s3-integration.txt` |
| `make check-docs` | 275 references, 0 problems | `m27-s3-check-docs.txt` |
| Scripted benchmark | 38 records, identical to approved baseline | `m27-s3-scripted.txt` |
| Integrity benchmark | 23/23 expected, identical to baseline | `m27-s3-integrity.txt` |
| Undo integrity group | 4/4 expected, identical to baseline | `m27-s3-integrity.txt` |
| Replay benchmark | 9/9 expected, identical to baseline | `m27-s3-replay.txt` |

`m27-s3-baseline-comparison.txt` compares every field, scalar type, key set and array order,
with no exclusions. Artifacts: `20261127T003451Z-scripted`, `20261127T003453Z-integrity`,
`20261127T003453Z-undo-integrity`, and `20261127T003455Z-replay`. All source and test changes
were present for the passing full suite, database run and benchmark checks. No SQL schema or
UI files changed, so their dedicated checks were not required.

S3 is complete. Stop for owner review/commit before S4. Suggested commit:
`feat(records): persist refusal observations and recover diagnostics`.

Limits: a crash before the first durable refusal proof retains the existing conservative
read-back behavior; unavailable historical guard evidence cannot be reconstructed. Already
terminal jobs are not backfilled. Snapshotless old steps keep their hashes and schemas, including
restore 1.0.0. Original snapshot propagation into undo remains S4, and the comparison UI remains
S5. No SQL schema or UI files changed. The existing B02 encoded-content gap is unchanged.
No paid calls, real connectors, staging, commits or pushes.


### S4 — Original evidence in undo and historical compatibility

Started from owner-committed S3, with explicit authorization to implement S4.
New assessment undo records persist the original preparation snapshot before dispatch, alongside
the existing prior rating and bound revision. The trusted app adapter validates the snapshot's
resource, subject, revision and declared fields; the resolver checks the source action hash
against the prepared arguments before saving it. Snapshots are immutable with the undo record.
They become usable for undo only through the existing confirmed-write/read-back arming path.

`crm.restore_assessment@2.0.0` carries that evidence in hashed arguments, labeled
`confirmed_original` with the source action hash. The original selected values, timestamp and
revision remain intact. That historical revision is deliberately distinct from the restore's
write guard, which still requires the revision left by the original write (`result_version`).
The target's conditional write, independent read-back, refusal outcome and single-use reversal
key remain unchanged. The new version declares and tests the same connector guarantees as v1.

Requests pin their selected restore version before run creation. Argument reconstruction and
execution identity checks use that version's schema. Previously saved v1 requests, unversioned
legacy requests/plans, approval hashes and audit identities keep their original semantics.
Restore v1 remains registered and executable. Model plans cannot name the trusted restore tool
or supply internal version pins. A changed source hash, subject, revision or field list cannot
substitute different evidence into an authorized restore.

Snapshotless records retain their old serialized shape and never gain a snapshot during recovery.
A new v2 restore of such a record has an explicit empty snapshot list; a v1 restore retains its
old arguments without a snapshot field. Existing S3 undo records also remain snapshotless even
if their original step had a preparation snapshot. No live read or reconstruction is presented
as evidence saved in an older undo record. Existing JSON storage suffices; no migration is needed.

Recovery after an undo record is saved but before dispatch intent now preserves its original
creation time as well as its snapshots. This avoids an identity conflict when recovery runs
later. Tests cover that delayed restart, original lost replies, restore-request and approval
restarts across a future registry upgrade, mixed assessment/restore versions, immutable storage,
provenance tampering and Postgres recovery using fresh store/service instances. A later record
edit is compared with the original evidence but still refuses the undo. Daily review and the
base resource-variable callback continue reading current state; original undo evidence does
not freeze those inputs or change policy rules.

Final checkpoint verification passed with zero failed attempts. Logs are ignored under
`bench-results/`:

| Check | Result | Log |
|---|---|---|
| Initial lint and mypy | Clean after routine import fixes; mypy 161 source files | `m27-s4-lint-1.txt`, `m27-s4-type-1.txt` |
| Focused tests | 156 passed, 9 database cases skipped here | `m27-s4-focused-1.txt` |
| Final undo-snapshot/recovery tests | 15 passed | `m27-s4-recovery.txt` |
| Final `make check` | Lint clean; mypy 161 files; 3,483 passed, 100 skipped, 541.12 seconds; 92% coverage | `m27-s4-check.txt` |
| `make test-integration` | 155 passed, isolated Postgres schemas | `m27-s4-integration.txt` |
| `make check-docs` | 275 references, 0 problems | `m27-s4-check-docs.txt` |
| Scripted benchmark | 38 records, identical to approved baseline | `m27-s4-scripted.txt` |
| Integrity benchmark | 23/23 expected, identical to baseline | `m27-s4-integrity.txt` |
| Undo integrity group | 4/4 expected, identical to baseline | `m27-s4-integrity.txt` |
| Replay benchmark | 9/9 expected, identical to baseline | `m27-s4-replay.txt` |

`m27-s4-baseline-comparison.txt` compares every field, scalar type, key set and array order,
with no exclusions. Artifacts: `20261127T005234Z-scripted`, `20261127T005236Z-integrity`,
`20261127T005236Z-undo-integrity`, and `20261127T005238Z-replay`. All source and test changes
were present for the passing full suite, database run and benchmark checks. No SQL schema or
UI files changed, so their dedicated checks were not required.

S4 is complete. Stop for owner review/commit before S5. Suggested commit:
`feat(undo): preserve original snapshot provenance`.

S4 does not add the comparison UI (S5) or the dedicated adversarial benchmark group (S6).
Historical missing evidence remains unavailable, and the existing B02 encoded-content gap
remains unchanged. No paid calls, real connectors, staging, commits or pushes.


### S5 — Safe API projections and approval/refusal cards

Started from owner-committed S4, with explicit authorization to implement S5.
Approval, step and matching verification views now expose `record_evidence`, a bounded display
projection of persisted snapshots and refusal observations. App declarations identify authorized
subjects and guarded revisions. The projector checks the prepared action hash, target set,
resource, revision binding, declared field types and diagnostic coverage/code before exposing
selected values. Undo provenance additionally requires the matching tenant-scoped immutable
undo record; the source hash alone does not establish provenance. Invalid or unavailable data
produces an unavailable view instead of an empty or misleading comparison.

Projection never invokes a target reader, retries an action, changes stored evidence or appends
audit events. Existing tenant access controls and original API argument/evidence fields remain.
Snapshots, observation times and partial coverage remain stable across refresh. Partially
observed batches retain every authorized target in order and explicitly mark unobserved targets.
Complete coverage means each target has an observation, which may still be missing or unreadable.

Approval cards show the saved selected fields separately from typed action arguments and collapsed
model-written prose. Refused steps show captured and observed columns, explicit changed/unchanged,
no-fields-tracked, snapshot-unavailable, observation-unavailable and record-missing states, plus
atomic-guard or later-read timing. Missing, present null, string values and unavailable values
have distinct displays. A matching later read is never described as proof of historical guard
values. Matching fields never permit execution; the refusal notice states that the action will
not be retried or sent for reapproval and that reassessment needs a fresh run.

Undo cards label evidence captured for the original confirmed action, retaining its source hash,
capture time and revision separately from the restore's required guard revision. Record strings
are rendered as text, with no HTML insertion or new action controls. The existing comparison
against the last approved call stays separate: snapshot changes are acknowledged without dumping
raw snapshot JSON or claiming the calls are identical. Generic snapshot blobs are replaced by
the validated record view; old API responses with missing evidence show an unavailable notice.

API tests cover saved approval values, every comparison state, missing/null distinctions, tenant
access, refresh stability, malformed/misbound diagnostics, tampered snapshots, partial replay
batches, old snapshotless restores and unavailable original-undo provenance. Existing Postgres
recovery tests now assert the new views through fresh service/store instances. UI card-model
tests cover all states, timing, provenance, safe text, coverage, old-response fallback and the
absence of retry/reapproval controls. Rendered-page tests remain the separate roadmap item 12.
The API contract is documented in `docs/api.md`.

Verification stopped after the second failed attempt, as required by the approved plan.
The first mypy attempt found a local variable reused with an optional type; renaming it fixed
that issue. The subsequent type and focused checks passed. The full suite then found one
existing API assertion incompatible with the new selected-field projection. It was left unfixed
at that stop and is resolved in the authorized continuation below.
No further code changes or verification runs were made after this stop; only this build record
and project memory were updated.

| Check | Result | Log |
|---|---|---|
| Initial lint | Clean after routine import fixes | `m27-s5-lint-1.txt` |
| Failure 1: initial mypy | Optional observation reused a non-optional loop variable; fixed | `m27-s5-type-1.txt` |
| Corrected mypy | Clean, 162 source files | `m27-s5-type-2.txt` |
| Focused API/approval/refusal/undo tests | 76 passed | `m27-s5-focused-1.txt` |
| Final UI check | TypeScript clean; 74 tests passed; production build passed | `m27-s5-ui-check-final.txt` |
| Failure 2: full `make check` | Lint/mypy clean; 1 failed, 3,499 passed, 100 skipped; 550.70 seconds; 92% coverage | `m27-s5-check.txt` |
| `make test-integration` | 155 passed, isolated Postgres schemas | `m27-s5-integration.txt` |
| `make check-docs` | 275 references, 0 problems (before the final build-record update) | `m27-s5-check-docs.txt` |
| Scripted benchmark | 38 records, identical to approved baseline | `m27-s5-scripted.txt` |
| Integrity benchmark | 23/23 expected, identical to baseline | `m27-s5-integrity.txt` |
| Undo integrity group | 4/4 expected, identical to baseline | `m27-s5-integrity.txt` |
| Replay benchmark | 9/9 expected, identical to baseline | `m27-s5-replay.txt` |

The remaining failure at that stop was
`tests/unit/test_decision_format.py::test_api_exposes_checks_as_ids_paths_operators_and_booleans`
at line 406. Its checks of the policy-check schema pass, but it then searches the entire run-view
JSON for absence of the signer, deal value and contract id. S5 intentionally puts selected deal
value and subject id in `record_evidence`, so the assertion now fails on `45000`. An authorized
continuation should update this regression to distinguish the new permitted projection from
policy checks and other redacted response fields, retaining the no-operand/no-private-data
assertions. Do not simply remove the privacy regression. Then rerun the full checkpoint.

`m27-s5-baseline-comparison.txt` compares every field, scalar type, key set and array order,
with no exclusions. Artifacts: `20261127T011413Z-scripted`, `20261127T011415Z-integrity`,
`20261127T011415Z-undo-integrity`, and `20261127T011417Z-replay`. All source and test changes
were present for these checks. No SQL schema changed, so a dedicated SQL run was not required.

S5 was not complete at that checkpoint. Work stopped for owner review/continuation before
fixing the remaining assertion. Suggested checkpoint commit:
`wip(records): add S5 evidence views; one API regression remains`.

No policy rules, approval identities, tool schemas, refusal semantics, connector writes or SQL
schema changed. The dedicated adversarial benchmark group and final milestone documentation
remain S6. The existing B02 encoded-content gap is unchanged. No paid calls, real connectors,
staging, commits or pushes.


#### Authorized S5 continuation from the stopped S5 checkpoint

The owner committed the stopped checkpoint and authorized S5 completion. The remaining API
regression now explicitly checks the permitted projection: the captured contract subject and
exactly the declared `deal_value` field, with no observation or comparison before dispatch.
It still verifies the exact policy-check schema, excludes the signer from the entire response,
and excludes argument values from every other part of the run response. The privacy regression
has been retained, with only the validated evidence projection exempted from the old assertion.
Production code and UI are unchanged in this continuation.

Final verification passed with zero failed attempts in this continuation:

| Check | Result | Log |
|---|---|---|
| Focused decision-format/evidence tests | 85 passed | `m27-s5-resume-focused.txt` |
| `make check` | Lint clean; mypy 162 files; 3,500 passed, 100 skipped, 563.77 seconds; 92% coverage | `m27-s5-resume-check.txt` |
| `make test-integration` | 155 passed, isolated Postgres schemas | `m27-s5-resume-integration.txt` |
| `make ui-check` | TypeScript clean; 74 tests passed; production build passed | `m27-s5-resume-ui-check.txt` |
| `make check-docs` | 275 references, 0 problems | `m27-s5-resume-check-docs.txt` |
| Scripted benchmark | 38 records, identical to approved baseline | `m27-s5-resume-scripted.txt` |
| Integrity benchmark | 23/23 expected, identical to baseline | `m27-s5-resume-integrity.txt` |
| Undo integrity group | 4/4 expected, identical to baseline | `m27-s5-resume-integrity.txt` |
| Replay benchmark | 9/9 expected, identical to baseline | `m27-s5-resume-replay.txt` |

`m27-s5-resume-baseline-comparison.txt` compares every field, scalar type, key set and array order,
with no exclusions. Artifacts: `20261127T014521Z-scripted`, `20261127T014523Z-integrity`,
`20261127T014523Z-undo-integrity`, and `20261127T014525Z-replay`. The test change was present
for every final check; only this build record and project memory changed afterwards. No SQL
schema changed, so a dedicated SQL run was not required. Rendered-page tests remain item 12.

S5 is complete. Stop for owner review/commit before S6, the dedicated adversarial benchmark
and final milestone documentation. No paid calls, real connectors, staging, commits or pushes.
Suggested continuation commit: `test(records): complete S5 privacy verification`.


### S6 — Dedicated adversarial benchmark and final documentation

The owner committed S5 and authorized S6. The new free command
`python -m bench record-changes` runs 18 fixed-plan cases through the shipped app assembly,
engine and temporary mock systems, with no model calls. It writes a separate manifest,
`trials.jsonl` and status under `*-record-changes`; the kind is `record_changes`, with zero spend.
Every case has an independent literal expected record. Exact comparison preserves scalar types,
key sets and array order; a mismatch is recorded and makes the command exit nonzero. Tests also
prove a deliberately wrong oracle fails, CLI exit propagation, artifact separation and strict
comparison. The original benchmark commands and group sizes are unchanged.

Cases cover selected-field changes; away-and-back edits whose fields match but revision refuses;
a later read that differs from guard-time state; missing/unreadable targets; snapshotless old
steps; absent diagnostics; original undo provenance and its absence; partial replay guards and
tenant movement; tampered snapshot hashes, foreign targets, diagnostic hashes, undeclared fields
and false complete coverage; recovery after saved refusal proof; and IAM's explicit empty field
set. Projection attacks operate on copied views, not immutable evidence. Each refused action is
checked for zero effects, one attempt, no open approval, valid audit chains and saved evidence
stable across refresh. Volatile identifiers are checked through binding/stability assertions
rather than emitted as benchmark outcomes.

Architecture, threat model (new T47 and corrected privacy wording), demo, README, results and
roadmap now describe the completed behavior and its limits. The demo is reproducible with the
free command and explains which saved rows establish each outcome. No engine, app, policy, UI or
SQL schema changes are part of S6.

Final verification passed on 2026-11-26 (America/New_York; artifact names use UTC):

| Check | Result | Log |
|---|---|---|
| Initial lint and type check | Clean after routine import fix; mypy 163 files | `m27-s6-lint-1.txt`, `m27-s6-type-1.txt` |
| Focused benchmark tests | 24 passed | `m27-s6-focused-1.txt` |
| `make check` | Lint clean; mypy 163 files; 3,524 passed, 100 skipped, 570.84 seconds; 92% coverage | `m27-s6-check.txt` |
| `make test-integration` | 155 passed, isolated Postgres schemas | `m27-s6-integration.txt` |
| `make ui-check` | TypeScript clean; 74 tests passed; production build passed | `m27-s6-ui-check.txt` |
| `make check-docs` | 280 references, 0 problems | `m27-s6-check-docs.txt` |
| New record-change benchmark | 18/18 literal expected records, zero spend | `m27-s6-record-changes.txt` |
| Scripted benchmark | 38 records, identical to approved baseline | `m27-s6-scripted.txt` |
| Integrity benchmark | 23/23 expected, identical to baseline | `m27-s6-integrity.txt` |
| Undo integrity group | 4/4 expected, identical to baseline | `m27-s6-integrity.txt` |
| Replay benchmark | 9/9 expected, identical to baseline | `m27-s6-replay.txt` |

`m27-s6-baseline-comparison.txt` compares every field, scalar type, key set and array order with
no exclusions. Artifacts: `20261127T020458Z-scripted`, `20261127T020502Z-integrity`,
`20261127T020502Z-undo-integrity`, `20261127T020505Z-replay`, and
`20261127T020505Z-record-changes`. All code and test changes were present for the final checks;
only documentation and project memory changed afterwards. Documentation validation and
`git diff --check` passed after the final documentation edits. There is no SQL schema change,
so a dedicated SQL schema run was not required.

One verification attempt failed: an independent comparison helper was accidentally invoked with
system Python, which does not support `zip(strict=True)`. It completed no comparison. Repeating
it with `.venv/bin/python` passed with no baseline differences. See
`m27-s6-comparison-attempt-1.txt` and `m27-s6-baseline-comparison.txt`. This counts as one failed
verification attempt; there were no product test failures.


S6 and M27 (roadmap item 11) are complete. Selected fields are not a whole-record history;
missing historical snapshots remain unavailable, and later reads cannot prove guard-time state.
Real connectors must supply the target guarantees demonstrated on the mocks. The B02
encoded-content gap is unchanged. Item 12's rendered-page tests remain separate and unstarted.
No paid model calls, real connectors, staging, commits or pushes. Stop for owner review/commit.
Suggested commit: `test(records): complete M27 adversarial coverage and documentation`.
