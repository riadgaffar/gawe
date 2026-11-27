# M26 build plan

Status: **Done.** Approved 2026-11-24; S1–S6 approved and committed by the owner and merged to `main` by fast-forward (2026-11-26). It implements D1–D5 in
[m26-design.md](m26-design.md), approved by the owner on 2026-11-24, each with the recommended
option. No paid model calls, except an optional UI pass in S6 that needs the owner's yes.

## Working rules and verification

Work on the branch `undo-record`. Do not commit, stage or push; the owner reviews and commits each
checkpoint. No live model calls and no real connectors. The design names the engine changes (the
`undo_records` store, migration 0010, the `undo` profile, the endpoint, the link event, and the
trusted-only tools in S3). Any engine change beyond those stops for the owner first.

Run S1–S6 in order and stop for owner review after each, with:

- the diff summary;
- the verification output, with a log in an ignored `bench-results/m26-sN-*.txt`;
- known limitations;
- a suggested one-line commit message.

If verification fails twice on the same step, stop and report. A change to approved semantics
needs a new owner decision; routine details may be settled here and recorded.

At each checkpoint run:

- `make check` and `make check-docs`;
- `python -m bench scripted` (38 records, identical) and `python -m bench integrity` (23 of 23,
  plus the new undo group from S5);
- `make test-integration` when a step touches code the Postgres tests exercise;
- `make ui-check` when the UI changes.

## S1. A current rating in the contract system (D2)

- **Contract record:** gains `risk_level` and `risk_summary`, absent in the seed (no rating yet).
- **`write_assessment` for 3.0.0:** in the guarded transaction it checks the bound revision, sets
  both fields, issues a new revision, and records the operation under GAWE's key together with
  the revision before and after. 1.0.0 and 2.0.0 keep their stores and behaviour, readable for
  verification.
- **`restore_assessment`:** sets both fields to given values only while the contract has a given
  revision, in one transaction, and issues a new revision. Recorded and readable by its own key.
  A second call under the same key changes nothing and returns the first result.
- **Declared guarantees:** 3.0.0 keeps 2.0.0's set. The restore declares `idempotent_by_key`,
  `authoritative_readback` and `conditional_write`. A new guarantee `reversible` is declared by
  the connector for 3.0.0 only. Every other write declares nothing new.
- **Test hooks:** another actor's change (exists); a change away and back (two changes that end on
  the original values).

Tests:

- conformance: a check for `reversible` (a write followed by a restore at the post-write revision
  returns the record to its prior fields, with a new revision), and the restore's three
  guarantees;
- the write moves the revision; another actor's change moves it again;
- the lost update: two writes bound to one revision, one lands and the other fails
  `record_changed`, also when racing;
- a restore after a change away and back is refused with `record_changed`;
- 1.0.0 and 2.0.0 writes still verify.

Suggested commit: `feat(connectors): the contract system keeps a current rating, and can restore it`

## S2. The record written before the change (D2)

- **Preparation** for `crm.write_assessment@3.0.0` reads the current rating, summary and revision
  in one read and adds `record.contract_version` (as today) and a trusted `prior` (rating and
  summary, absent when there's none) inside the hash. A proposal can't supply `prior`; the field
  classes are `prior.risk_level` typed and `prior.risk_summary` free text.
- **The app manifest** moves to 3.0.0 for new plans; 2.0.0 stays registered for verification.
  Rules that match `crm.write_assessment` keep matching (they match the tool, not the version);
  the policy load checks confirm this at startup.
- **Reversibility** is declared per (tool, version) in the app bundle (`undoable`), and startup
  refuses an `undoable` entry whose connector doesn't declare `reversible`, as M22 does for other
  guarantees.
- **`undo_records`** (in memory, and Postgres with migration 0010): one row per (tenant, run,
  step), keyed like the expiry job and added in `VerificationService.prepare` before the read-back
  intent, so a dispatched write always has one. It holds the action hash, the operation key, the
  prior fields and the bound revision. It is armed only when read-back agrees and observes the
  post-write revision; it then holds that revision. A mismatch or unknown outcome leaves it
  unarmed for good, with the reason.
- **The approval card** shows `prior.risk_level` next to the new rating, typed, and the prior
  summary collapsed as free text. A card-model test covers it.

Tests:

- every 3.0.0 write has its record before dispatch, including after a crash between the record and
  the intent (adding it again is a no-op);
- armed only on agreement; disagreement and unknown leave it unarmed with the reason;
- `prior` is exactly the replaced state, also when another actor changes the contract between
  preparation and the write (the write then fails, and the record is never armed);
- a model-supplied `prior` never reaches policy;
- tenant isolation of the store; the Postgres contract tests for the new table.

Suggested commit: `feat(undo): an undo record written before each rating write`

## S3. The reversal (D3, D4)

- **Trusted-only tools:** a manifest can be marked trusted-only. The Planner's catalog leaves it
  out, and plan validation refuses a plan that names it (`unknown_tool`), so no model output can
  reach it. `crm.restore_assessment@1.0.0` is the first one.
- **Undo request:** `POST /v1/runs/{run_id}/steps/{step_id}/undo`, for a `case_manager` in the
  run's tenant. Checks, in order, each a refusal with no run started: another tenant's step
  (`not_found`), not an undoable (tool, version) (`not_reversible`), no armed record
  (`undo_unavailable`), already undone or undo pending (`already_undone`), the step belongs to an
  undo run (`undo_of_undo`).
- **The undo run:** profile `undo`, created with its plan already set by trusted code (one step,
  the restore), so the Planner and Reader are never called. `POST /v1/runs` refuses profile `undo`.
  Preparation fills the restore's arguments from the armed record: contract, prior fields,
  post-write revision. Its key derives from the original operation key (`gawe.undo.v1`), so a
  repeated request is the same operation.
- **Policy:** the restore goes through the app's rules like any write. Restoring to "low" needs
  legal (`low-risk-needs-legal` extended to the restore); restoring to nothing (no prior rating)
  needs legal too, since it removes a rating. Other ratings follow the existing rating rules.
  Read-back by the restore's key.
- **Audit:** the undo run's own chain, and a post-terminal `step.undo_requested` and
  `step.undone` (or `step.undo_failed` with its code) on the original run, linking run and step ids
  and hashes. The armed record is marked used when the restore reads back.
- **Later steps:** the undo view lists steps in the original run that took this step's output by
  `$ref`, with their tool and outcome, and says their effects stay.

Tests:

- end to end: write high→low with legal approval, undo restores high automatically; write
  low→high, undo to low waits for legal;
- change away and back between the write and the undo is refused at the target, nothing written;
- two undo requests at once make one run and one change;
- an undo racing another write: one wins, the other fails `record_changed`;
- every refusal code, and another tenant's step;
- the Planner can't plan the restore, and a document asking for undo changes nothing;
- `POST /v1/runs` with profile `undo` is refused;
- a crash between the request and the run, and between dispatch and read-back, recovers to one
  restore;
- the link events are on the original run and its chain verifies.

Suggested commit: `feat(undo): a person can undo a rating through its own guarded run`

## S4. The UI (D4)

- **Run page:** an Undo button on a step whose record is armed and unused, for a `case_manager`.
  Unavailable steps show why (the refusal code in words).
- **Undo card:** the prior and current rating, the revision check in words ("the contract hasn't
  changed since this rating was written"), and later steps that used the output.
- **The restore's approval card** uses the existing card, with the prior rating typed.
- Card-model tests and `make ui-check`. A rendered-page test stays out, as queue item 11.

Suggested commit: `feat(ui): undo a rating from the run page`

## S5. Adversarial suite and benchmarks

- **Boundary registry and independent oracle** for the extended rules (restore to low, to nothing,
  to each other level).
- **Unreadable-input targets** for the restore and for `prior`: an unreadable current rating at
  preparation fails closed (`record_version_unavailable`); an unreadable record at undo time is
  `undo_unavailable`.
- **Integrity scenarios, their own group:** undo after a change away and back; undo of an unknown
  outcome; double undo; undo racing a write. The 23 existing scenarios and the 38 scripted records
  stay identical; any change is reported, not absorbed.
- **D5 counts:** per tenant, armed records, used records, and unavailable ones by reason, as a
  counts-only telemetry metric. No content.

Suggested commit: `test(undo): adversarial and integrity cases for undo`

## S6. Verification and docs

- Full checkpoint set, plus `make test-integration` on Postgres, recorded in the build record.
- **A manual pass through the UI is optional and needs the owner's yes:** the API only runs with
  the real model, so a contract run to undo costs a little (one Reader and one Planner call). It
  is not needed: the API tests drive the same flow with a fake model.
- **Docs:** a new threat-model row (undo applied over a later change, or to an effect that can't
  be reversed), the T31 line (a real CRM must move the revision on its own writes), API docs for
  the endpoint and the refusal codes, architecture, the demo script (an undo walkthrough), the
  roadmap and the design's build record. Credit the r/LLMDevs readers.
- The public port only when the owner asks.

Suggested commit: `docs: record verified M26 results`
