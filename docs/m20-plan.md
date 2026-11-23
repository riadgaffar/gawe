# M20 build plan

Status: **Done.** S1–S5 approved and committed by the owner and merged to `main` by fast-forward
(2026-11-22). Plan approved by the owner on 2026-11-22.
It implements D1–D5 in [m20-design.md](m20-design.md), approved by the owner on 2026-11-22. No
paid model calls.

## Working rules and verification

Work on `prepare-only`. Do not commit, stage or push; the owner reviews and commits each
checkpoint. No live model calls and no real connectors. Nothing is prepare-only by default, so
existing behaviour, the scripted benchmark and the integrity suite must not change.

Run S1–S5 in order and stop for owner review after each, with the diff summary, the verification
output (and a log in an ignored `bench-results/m20-sN-*.txt`), known limitations and a suggested
one-line commit message. If verification fails twice on the same step, stop and report. A change
to approved security semantics needs a new owner decision; routine details may be settled here
and recorded.

At each checkpoint run `make check` and `make check-docs`, `python -m bench scripted` (identical
records) and `python -m bench integrity` (23 of 23). For schema changes also `make test-schema`,
an Alembic upgrade and downgrade on a throwaway database, and `make test-integration`. For UI
changes, `make ui-check`.

## S1. The setting and the guard at dispatch (D1)

- `AppBundle.prepare_only`: a mapping from (tool, version) to the role that marks the hand-off.
  Empty for both shipped apps. `load_app_rules` refuses a setting that names a tool version the
  app doesn't register, a read tool, or a role no manifest or rule knows.
- The executor, before anything else in `execute`: if the step's (tool, version) is in the
  **current** setting, it refuses with `prepare_only` and never reaches the tool, whatever the
  stored decision, approval or step status says. This is the guarantee; everything later only
  makes the run honest about it. In S1 the refusal fails the step closed; S2 turns it into the
  hand-off.
- The same check guards `reconcile` paths that could dispatch (none do today; a test pins that).

Tests, each a path to dispatch, each refused for a prepare-only step with the tool never called:
- an allowing rule;
- `needs_approval` with the approval granted;
- an approval granted before the setting existed (the decision and approval stored first, the
  setting added after);
- a resumed run after a pause;
- recovery after a crash at every point the M16a crash tests cover;
- the read-back and review sweeps running over the step;
- a retried step after a pre-dispatch rejection;
- a direct `Executor.execute` call with a valid signed `allow` decision;
- the benchmark harness's honest approvals.

Plus: a tool outside the setting behaves exactly as before; startup refuses each bad setting.

Suggested commit: `feat(engine): refuse dispatch for prepare-only actions at the executor`

## S2. The hand-off states (D3, D4)

- Statuses: step `prepared`, `done_by_person`, `not_done_by_person`; run `waiting_for_person`.
  Transition table entries; migration 0009 (SQL and Alembic) widening the status checks and
  adding step columns `handoff_role`, `handoff_by`, `handoff_at`, `handoff_reference` (nullable;
  reference at most 120 characters of letters, digits and `.`, `_`, `:`, `-`).
- Orchestrator: a prepare-only step is resolved, prepared and checked by policy as today, so its
  action hash binds the preparation; then, instead of awaiting approval or executing, it moves to
  `prepared` and the run to `waiting_for_person`. A denied one stays denied: prepare-only never
  weakens a deny. A `needs_approval` outcome is recorded but asks no approval.
- Marking: a person holding the role, not the run's requester, marks it done (optional reference)
  or not done, naming the action hash; a stale hash, a wrong role, the requester, or a second mark
  is refused. Done counts as success for `depends_on`; not done fails dependants as upstream; a
  `$ref` to its output fails with `handoff_has_no_output`.
- Audit: `handoff.prepared`, `handoff.done`, `handoff.not_done`, with hash, role, user and the
  reference's length (never its text, as other free fields).
- The next-day review leaves hand-offs out of "automatic actions" and records nothing for them.

Tests: the transitions; marking rules; dependants; output refs; audit payloads; a crash between
preparing and marking; the store round trip on memory and Postgres; the migration up and down.

Suggested commit: `feat(engine): hand prepare-only actions to a person and record what they did`

## S3. API and UI

- API: `GET /v1/handoffs` (what the caller's roles may mark, in their tenant), `POST
  /v1/handoffs/{run_id}/{step_id}` with `{outcome: done | not_done, action_hash, reference?}`. The
  run view shows hand-offs and who marked them.
- UI: a hand-off card next to approvals, reusing the approval card's typed-first layout and
  collapsed model text, with Done (and an optional reference) and Not done.

Tests: the endpoints' rules as in S2, tenant scoping, the UI model and source tests as for the
approval card.

Suggested commit: `feat(api,ui): list and mark hand-offs`

## S4. Benchmark and demo (D5)

- Harness: a `prepared` attack step is recorded as `stopped_by: prepared`; it is never approved as
  honest. A free scripted variant with signatures prepare-only, run in tests only: every signature
  attack stops at `prepared`, every other record matches the normal scripted run.
- Demo docs: how to make sending for signature prepare-only, and what the hand-off looks like.

Suggested commit: `feat(bench): count prepared steps and show a prepare-only configuration`

## S5. Verification and completion record

- Review the cumulative diff for scope, compatibility (no setting means no change; migration 0009
  up and down) and privacy (no arguments or references in audit or logs).
- Negative matrix on exported copies, each with a test that fails when the guard is removed, after
  a control run: the executor guard; the guard reading the stored setting instead of the current
  one; the requester allowed to mark; a stale hash accepted; a hand-off output usable by `$ref`;
  the harness approving a prepared step.
- Every check above and both benchmarks.
- Threat model (a new row), API docs, results page, roadmap, both completion records.

Suggested commit: `docs: record verified M20 results`

## Approval and progress record

- Phase 2: design D1–D5 and the name M20 approved by the owner on 2026-11-22.
- Phase 3: this plan, approved by the owner on 2026-11-22.
- S1: built on `prepare-only`, waiting for owner review.
  - `AppBundle.prepare_only` ((tool, version) to the role that marks it), empty for both shipped
    apps; startup refuses an unregistered or retired tool version, a read tool, or a role no
    manifest or rule knows.
  - The executor refuses `prepare_only` before checking any decision or approval, against the
    setting it runs with now. A step with an operation intent from before the setting is only read
    back: the reconcile path never dispatches. Settled here: that read-back is allowed, because it
    reports what already happened and sends nothing.
  - Tests (`tests/unit/test_prepare_only.py`, one in `tests/bench/test_injection.py`): refused, with
    the tool never called, for an allowing rule; an approval granted; an approval granted before
    the setting; a resumed run; repeated advances with the sweeps ticking; a direct executor call
    with a valid signed `allow`; recovery after a crash at the intent, attempt and accept
    boundaries; and the benchmark harness's honest approval. A tool outside the setting runs as
    before; each bad setting stops startup.
  - `make check` 2453 passed, 76 skipped; Postgres 116 passed; scripted 38 of 38 identical;
    integrity 23 of 23.
- S1: approved by the owner and committed.
- S2: built, waiting for owner review.
  - States: step `prepared`, `done_by_person`, `not_done_by_person` (both terminal); run
    `waiting_for_person`. Migration 0009 widens both status checks and adds `steps.handoff`
    (jsonb: role, by, at, reference), required exactly for the hand-off states (a cancelled one
    keeps it when it fails). Settled here: one `handoff` record instead of four columns. Upgrade,
    downgrade and upgrade again on a throwaway database pass; downgrade refuses while hand-offs
    exist.
  - Orchestrator: a prepare-only step is resolved, prepared and judged by policy as before (a deny
    stays a deny), then moves to `prepared` with `handoff.prepared` audited, and asks no approval.
    The run waits for a person; marking it lets the run go on. Done counts as success for
    dependants; not done ends the run as `denied` with `handoff_not_done`, like a rejection
    (settled here); a `$ref` to its output fails with `handoff_has_no_output`. Crash recovery
    lists `waiting_for_person` among active runs.
  - `Orchestrator.mark_handoff`: refused unless the step is still prepared, the hash is the
    current one, the person holds the role and isn't the requester, and the reference matches
    `^[A-Za-z0-9._:-]{1,120}$`; marked once. Audited by user with hash, role and the reference's
    length only.
  - The next-day review already re-checks rules and samples only executed steps, so hand-offs get
    its integrity checks but no rule or sample items. No change needed.
  - Tests: 29 in `test_prepare_only.py` (the S1 paths now end handed off, the marking rules,
    dependants, output refs, audit payloads, a crash after preparing, cancelling), a store contract
    test run on memory and Postgres, `tests/sql/test_handoff.sql` (7 checks); the state-machine and
    recovery guard tests now cover the new states. `make check` 2471 passed, 77 skipped;
    `make test-schema` 108 checks; Postgres 118 passed; scripted 38 of 38 identical; integrity 23
    of 23.
- S2: approved by the owner and committed.
- S3: built, waiting for owner review.
  - API: `GET /v1/handoffs` lists prepared steps in the caller's tenant whose role they hold, on
    runs they didn't start, with the approval view's typed fields and collapsed free text, the
    hash, role, policy outcome and preparation. `POST /v1/handoffs/{run_id}/{step_id}` takes
    `{outcome: done | not_done, action_hash, reference?}` (strict body) and kicks the run.
    Refusals keep S2's codes: `role_required` and `requester_cannot_mark` are 403,
    `action_hash_mismatch` and `not_waiting_for_a_person` 409, `reference_invalid` 422; another
    tenant's run or a step never handed off is 404. The run view's steps carry `handoff` (role,
    who marked it, when, reference).
  - UI: a hand-off card under the approvals on the inbox page, typed fields first, hash, free text
    collapsed, with a reference box, Done and Not done; nothing on it sends. The run page notes a
    prepared step and shows who marked each hand-off.
  - Settled here: the listing reads up to 500 waiting runs across tenants, oldest first, then
    filters by tenant, as the approval inbox reads all open approvals.
  - Tests: 15 in `tests/api/test_http_handoff.py` (who sees it, done and not done end to end, each
    refusal's status code, a strict body, a second mark, not found, tenant scoping, no hand-off
    without the setting); 5 UI tests (the card model, the run view's summary, tones, the card's
    source: collapsed free text, no raw arguments, only done and not done).
  - `make check` 2486 passed, 77 skipped; `make ui-check` passes (34 tests, build); scripted 38 of
    38 identical; integrity 23 of 23. No schema change, so no Postgres or migration run.
- S3: approved by the owner and committed.
- S4: built, waiting for owner review.
  - Harness: a step left `prepared` is recorded as `stopped_by: prepared` (read before the
    harness cancels the run, which it now does for `waiting_for_person` as for a pause). The
    harness never marks a hand-off, and a prepared step never reaches the inbox, so it is never
    approved as honest. Settled here: `interventions.approval_requested` no longer counts a
    prepared step, since it asked no approval; its rules are still listed. The report, metrics
    order and adaptive descriptions know the new label.
  - Scripted variant with signatures prepare-only, in `tests/bench/test_scripted.py` only: all 3
    signature attacks move from `approval_pending` to `prepared`, nothing is sent, and apart from
    that label, `paused_for_approval` and the held count, their records are unchanged; the other
    35 records are identical to the normal run.
  - Demo: `gawe.apps.contract_review:signatures_by_hand` (the demo app with signatures
    prepare-only, marked by `legal_reviewer`; the default `bundle` stays empty), and section 5d of
    `docs/demo-script.md`. A test runs the section's flow without a model: C-1002 is prepared,
    policy records finance `needs_approval` but asks nobody, Bob marks it done, the run completes
    and no envelope is sent.
  - Found and fixed: the run page's "Needs" column showed the policy's approver role before the
    hand-off's role; it now shows the role that marks it.
  - `make check` 2489 passed, 77 skipped; `make ui-check` passes (35 tests, build); scripted 38 of
    38 identical to S3's run; integrity 23 of 23.
- S4: approved by the owner and committed. Two of its limits were then closed at the owner's
  request, before S5:
  - The prepare-only variant test now also runs the 40 hand-written attacks: all 5 signature
    attacks move from `approval_pending` to `prepared` and nothing is sent; every other record is
    identical to the normal run.
  - A walk-through in a browser, with the API serving a test environment (no model) and the UI's
    dev server: Bob saw the assessment hand-off, the browser refused a bad reference, Done with
    `ENV-2041` let the dependent signature become a hand-off of its own, Not done ended the run
    `denied`; the run page showed the waiting notice, who marked each step and the reference; the
    audit page listed `handoff.prepared`, `run.waiting_for_person` and `handoff.not_done` by Bob.
  - It found three UI faults, now fixed: Not done was blocked by a half-typed reference (the
    browser's field check, then the server action); and the card's policy line ("Deals over
    100,000 need finance approval") read as if someone still had to approve. It now says the
    policy would need an approval, that none is asked, and that the person decides.
  - `make check` 2490 passed, 77 skipped; `make ui-check` passes (38 tests, build); scripted 38 of
    38 identical; integrity 23 of 23.
- S4 follow-up: approved by the owner and committed.
- S5: cumulative review, negative matrix (6 of 6 caught, control passed) and final verification.
  `make check` 2490 passed, 77 skipped; `make test-integration` 118 passed; `make test-schema` 108
  checks; Alembic 0008 to 0009 and back on a throwaway database; `make ui-check` 38 tests; scripted
  identical to M19's last run; integrity 23 of 23. Threat model (T38), results page (M20 section),
  roadmap (M20 row, queue item 5 done, Next) and the design's build record updated; API docs since
  S3. Logs in `bench-results/m20-s5-*.txt`. Waiting for owner review.
- S5: approved by the owner and committed, then merged to `main` by fast-forward.
