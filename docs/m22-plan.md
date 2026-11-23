# M22 build plan

Status: **Done.** S1–S3 and the limits follow-up approved and committed by the owner and merged to
`main` by fast-forward (2026-11-23). Plan approved by the owner on 2026-11-22.
It implements D1–D5 in [m22-design.md](m22-design.md), approved by the owner on 2026-11-22. No
paid model calls.

## Working rules and verification

Work on a branch `target-guarantees`. Do not commit, stage or push; the owner reviews and commits
each checkpoint. No live model calls and no real connectors. No migration is planned; if one
turns out to be needed, it stops for the owner first.

Run S1–S3 in order and stop for owner review after each, with the diff summary, the verification
output (and a log in an ignored `bench-results/m22-sN-*.txt`), known limitations and a suggested
one-line commit message. If verification fails twice on the same step, stop and report. A change
to approved semantics needs a new owner decision; routine details may be settled here and
recorded.

At each checkpoint run `make check` and `make check-docs`, `python -m bench scripted` (identical
records) and `python -m bench integrity` (23 of 23); `make test-integration` when a step touches
code the Postgres tests exercise. A scripted or integrity change is a finding, reported before
anything else.

## S1. Guarantees declared, and required at startup (D3, D4, D5)

- `AppBundle.guarantees`: (tool, version) to what its connector guarantees: `conditional_write`,
  `idempotent_by_key`, `authoritative_readback`, `atomic_reservation`. It replaces
  `conditional_writes`. The mocks declare what they implement (every shipped write: idempotent by
  key and read back from the system's own record; the three 2.0.0 writes: conditional too).
- What each current write needs, derived at startup: idempotency by key and an authoritative
  read-back for every write; a conditional write for one that binds a record (today's check,
  moved here); atomic reservation for one that a rule judges with a running total.
- A new policy source kind, `counter`: a running total kept by a target system. A write needs
  atomic reservation when any rule for its tool reads a variable that depends, directly or not,
  on a `counter` source (the same walk M15's provenance check does).
- A missing guarantee stops startup, naming the tool version and the guarantee, unless that tool
  version is prepare-only (M20), since GAWE then never sends it.
- Retired versions are not required to declare anything (they never dispatch); the existing
  read-back pairing check stays as it is.

Tests: both shipped apps declare everything their writes need; removing each declared guarantee
from each write stops startup with its name; a record-binding write without a conditional write
still stops startup (the old test, now through the new check); a test app whose rule reads a
`counter` source starts only with atomic reservation declared, or with the class prepare-only; a
read tool needs nothing.

Suggested commit: `feat(engine): require the guarantees each write relies on at startup`

## S2. Conditions checked just before dispatch (D2)

- A declared dispatch condition: a name and a check that reads the authoritative system and says
  whether the condition still holds. Declared per (tool, version) in `guarantees`, as
  "checked, not enforced".
- The IT app declares `ticket_open` for `iam.grant_access@2.0.0`: the run's ticket is still open
  in the ticket system.
- The executor runs each declared condition last, after every other check and just before
  calling the tool, on every attempt. If one no longer holds, or can't be read, nothing is sent:
  the write's intent is closed as not dispatched (as for a missing approval today) and the step
  fails with `condition_changed` or `condition_unreadable`. Recovery and read-back never run it;
  they don't dispatch.
- Audit: `condition.checked` on the run before each dispatch (step, action hash, condition names,
  `held` or the failed name), never the values read. The run view lists, per step, the conditions
  that were only checked.

Tests: the probe as a test: a ticket closed while the grant waits for approval is refused at
dispatch, no grant; a ticket closed and reopened before the approval: granted; a ticket closed
between the check and the write (the named race): granted, and the audit shows the check held;
an unreadable ticket at dispatch: refused, nothing sent; a retry checks again; a crash after the
check and before the call recovers without sending; a grant on a direct executor call with a
valid decision and approval is still checked; contract-review writes, which declare no condition,
behave exactly as before.

Suggested commit: `fix(engine): re-check the ticket just before a grant is sent`

## S3. Verification and completion record

- Review the cumulative diff for scope, compatibility (both shipped apps start; contract review
  unchanged; scripted and integrity identical) and privacy (no values read by a condition in the
  audit trail or logs).
- Negative matrix on exported copies, each with a test that fails when the guard is removed, after
  a control run: the dispatch condition skipped; the condition checked before the approval
  instead of just before dispatch; a missing guarantee allowed at startup; the prepare-only
  exemption widened to every class; a counter source not traced through a variable.
- Every check above and both benchmarks.
- Docs: threat model (a new row for stale conditions, with the named window), API docs (the run
  view field, the new codes, the guarantees an app declares), roadmap (M22 row, queue item 8 split
  with M23 queued), both completion records.

Suggested commit: `docs: record verified M22 results`

## Approval and progress record

- Phase 2: design D1–D5 and the name M22 approved by the owner on 2026-11-22.
- Phase 3: this plan, approved by the owner on 2026-11-22.
- S1: built on `target-guarantees`, waiting for owner review.
  - `gawe.core.guarantees`: the four guarantees, and `required(...)`, what a current write relies
    on: idempotency by key and an authoritative read-back for every write, a conditional write
    for one that binds a record, atomic reservation for one that a rule selecting it judges with
    a variable depending, directly or through other variables, on a `counter` source. Reads and
    retired versions need nothing.
  - `AppBundle.guarantees` replaces `conditional_writes`. Startup refuses a declaration for an
    unregistered tool version or an unknown guarantee, and a current write missing anything it
    relies on, naming the tool version and the guarantees, unless the version is prepare-only.
    The old record-binding check now runs through this one.
  - `PolicySource.kind` gains `counter`. No shipped app uses one.
  - The mocks declare what they implement: the three 2.0.0 writes all three guarantees, their
    1.0.0 versions idempotency and read-back, `email.send@2.0.0` idempotency and read-back. The
    test app in `tests/api_bundle.py` declares idempotency and read-back, which its fake connector
    provides; it was the one place startup now refused.
  - Tests: 9 in `tests/unit/test_guarantees.py` (every shipped write declares what it needs;
    removing any needed guarantee stops startup; prepare-only needs none; reads and retired need
    none; bad declarations; a running total read directly or through a variable needs atomic
    reservation, or prepare-only; an unread running total needs nothing). The record-binding test
    now removes `conditional_write` from the declarations.
  - `make check` 2532 passed, 79 skipped; `make test-integration` 121 passed; scripted 38 of 38
    identical; integrity 23 of 23.
- S1: approved by the owner and committed.
- S2: built, waiting for owner review.
  - `DispatchCondition` (a name and a check that reads the authoritative system) and
    `AppBundle.dispatch_conditions`, per tool version. Settled here: a field of its own beside
    `guarantees`, since a condition is a check GAWE runs, not something the target promises.
    Startup refuses conditions on anything but a current write, and names that aren't distinct
    and short.
  - The IT app declares `ticket_open` for `iam.grant_access@2.0.0`: the run's ticket is still
    open in the ticket system (a run without one doesn't hold it).
  - The executor checks the step's conditions on every attempt, after the attempt is opened and
    immediately before calling the tool. If one no longer holds or can't be read, nothing is
    sent: the attempt closes as failed, the write's intent as `not_dispatched`, and the step fails
    with `condition_changed` or `condition_unreadable`. Recovery and read-back never reach it.
  - Audit: `condition.checked` (step, action hash, condition names, `held` or `changed:`/
    `unreadable:` and the name), never a value read. The run view's steps carry
    `checked_not_enforced` (the condition names); the UI type has the field, the page doesn't
    show it yet.
  - The test fixture that rebuilds the pre-M16b app now drops conditions too (there were none).
  - Not added, as planned: a direct `Executor.execute` call. The check is inside the executor's
    write path, which every caller reaches; the tests drive it through approvals and recovery.
  - Tests: 16 in `tests/unit/test_dispatch_conditions.py` (the gap: closed while waiting,
    refused, nothing sent; reopened before approval, granted; the named race, closed between the
    check and the write, granted with the check recorded as held; unreadable, refused; a retry
    checks again; closed before a retry, refused; the audit payload; the run view; contract
    review unchanged; a crash after the check never sends on recovery; the startup refusals; the
    IT app's declaration).
  - `make check` 2548 passed, 79 skipped; `make test-integration` 121 passed; UI typecheck and 38
    tests; scripted 38 of 38 identical; integrity 23 of 23.
- S2: approved by the owner and committed.
- S3: cumulative review, negative matrix (5 of 5 caught, control passed; one new test,
  `test_one_prepare_only_action_exempts_only_itself`, added because nothing caught the widened
  exemption) and final verification. Settled here: the plan's "checked before the approval"
  mutation is the same as skipping the check (preparation already reads the ticket), so "checked
  once before the attempts" was tested in its place. `make check` 2549 passed, 79 skipped;
  `make test-integration` 121 passed; `make ui-check` passes; scripted identical; integrity 23 of
  23. Threat model (T39), API docs ("What targets guarantee", the run view field), roadmap (M22
  row, item 8 split, Next M23) and the design's build record updated. Logs in
  `bench-results/m22-s3-*.txt`. Waiting for owner review.
- S3: approved by the owner and committed. Two of its limits were then closed at the owner's
  request, before the merge:
  - The run page shows, under each step, "Checked just before sending, not enforced by the
    target" with the condition names (`conditionsNote`), with a UI test.
  - `tests/unit/test_guarantee_conformance.py` runs every declared guarantee against the mock that
    declares it, with arguments the engine prepared: the same key twice gives one effect; the
    operation reads back by its key and a key never sent doesn't; a stale record revision is
    refused with nothing written. 17 cases match the declarations exactly, and a declaration
    with no check fails. Against a copy with the IAM's revision check removed it failed the IAM
    case; with a store that keeps each call under a new id, every case on that store.
  - `make check` 2568 passed, 79 skipped; `make test-integration` 121 passed; `make ui-check` 39
    tests and the build; check-docs 213 references; scripted identical; integrity 23 of 23.
- The limits follow-up: approved by the owner and committed, then merged to `main` by
  fast-forward.
