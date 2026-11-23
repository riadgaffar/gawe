# M21 build plan

Status: **Done.** S1–S4 approved and committed by the owner and merged to `main` by fast-forward
(2026-11-22). Plan approved by the owner on 2026-11-22.
It implements D1–D4 in [m21-design.md](m21-design.md), approved by the owner on 2026-11-22. No
paid model calls.

## Working rules and verification

Work on a branch `old-grant-end-dates`. Do not commit, stage or push; the owner reviews and commits
each checkpoint. No live model calls and no real connectors. Nothing changes until an operator runs
the command with `--apply`, so existing behaviour, the scripted benchmark and the integrity suite
must not change.

Run S1–S4 in order and stop for owner review after each, with the diff summary, the verification
output (and a log in an ignored `bench-results/m21-sN-*.txt`), known limitations and a suggested
one-line commit message. If verification fails twice on the same step, stop and report. A change
to approved semantics needs a new owner decision; routine details may be settled here and
recorded.

At each checkpoint run `make check` and `make check-docs`, `python -m bench scripted` (identical
records) and `python -m bench integrity` (23 of 23). When a step adds a Postgres query, also
`make test-integration`. No migration is planned; if one turns out to be needed, it stops for the
owner first.

## S1. The mock IAM can read and revoke old grants

- `MockIAM.grant_state` reads all three places a grant can be: the 2.0.0 store, the 1.0.0 store
  (M16a to M16b) and the ledger (M9 to M15). An old grant is `active` until revoked; it has no end
  date of its own, so it is never `expired` by the system's rule.
- `revoke` already records any key; a revoked old grant now reads back as `revoked`.
- `overdue` stays the system's own count (2.0.0 grants past their end date). Old grants are counted
  from GAWE's jobs instead (S3), since the system doesn't know their dates.
- The it-access bundle registers its IAM as the revoker for `iam.grant_access@1.0.0` too. The
  startup check that pairs revokers with writes that carry an end date allows a revoker for a
  retired version of the same tool, and nothing else.

Tests: a ledger-only grant and a 1.0.0-store grant each read as active, then revoked after a
revoke; a revoke twice records once; a key no store has reads as absent; 2.0.0 behaviour
unchanged; the startup check still refuses an unexpected revoker.

Suggested commit: `feat(connectors): let the mock IAM revoke grants made before M16b`

## S2. Finding old grants and their end dates (D1, D4)

- A store query for steps of one tool version across tenants: `StepStore.list_for_tool(tool,
  version)`, on memory and Postgres, with a contract test. No schema change.
- `old_grants(...)`: each `iam.grant_access@1.0.0` step with a succeeded execution, or in
  `verification_unknown` or `verification_blocked`. For each: tenant, run, step, the key the
  target knows it by (the execution's idempotency key, which covers both kinds of old grant), and
  when it was made (the execution's finish time, or its start when it never finished).
- The end date rule, a pure function: `max(made + lifetime, backfill + notice)`, naming which side
  won (`lifetime` or `notice`). The lifetime is the one the app grants today (the it-access
  bundle declares it); the notice is 7 days by default, 1 to 30.

Tests: the rule at both sides and the boundary; the notice range refused outside 1 to 30; old
grants found from each kind of history (pre-M16a key, M16a-era key); a failed or never-dispatched
step and every 2.0.0 grant left out; another tenant's grants kept apart; the store query on memory
and Postgres.

Suggested commit: `feat(maintenance): find grants made before M16b and give each an end date`

## S3. The command and the expiry (D2, D3)

- `python -m gawe.maintenance end-old-grants [--apply] [--notice-days N]`, against the durable
  stores like `sweep --once`.
  - Dry run (default): one JSON line per grant (tenant, run id, step id, end date, which rule set
    it, whether a job already exists), then totals. No email, system or level. Writes nothing.
  - `--apply`: for each grant, a `grant_expiry` job due at its end date (deterministic id, so a
    second run adds nothing), then `grant.end_date_set` on the grant's run (step, end date, rule,
    notice days). A crash between the two is repaired by the next run, which audits a job that
    has no event yet, once.
- The job is M16b's, with the end date from the backfill instead of the step's arguments. It
  revokes through the 1.0.0 revoker and finishes when the IAM system reads the grant back as
  revoked or absent, then audits `grant.revoked` as today.
- Overdue: `GrantExpiryJobs.overdue` adds old-grant jobs past their due time and not finished, so
  a stalled revoke raises the same alarm.

Tests: the dry run writes nothing and prints no personal data; apply then apply again adds one job
and one event per grant; a crash between job and event; the sweep revokes a ledger-only and a
1.0.0-store grant and reads them back; a grant already removed ends as not granted; an unknown
outcome that never landed ends as not granted; a revoke the system ignores stays waiting and
counts as overdue; a grant from a cancelled run is ended too; nothing is revoked before its date;
the command refuses `GAWE_STORE=memory` as the sweep does.

Suggested commit: `feat(maintenance): end grants made before M16b with an operator command`

## S4. Verification and completion record

- Review the cumulative diff for scope, compatibility (nothing changes without `--apply`; 2.0.0
  grants untouched) and privacy (no email, system or level in the output, logs or audit).
- Negative matrix on exported copies, each with a test that fails when the guard is removed, after
  a control run: the dry run writing; the notice ignored (end date from the lifetime alone); old
  grants read only from the 2.0.0 store (revoke never read back); old-grant jobs left out of the
  overdue count; a failed step treated as a grant; the backfill auditing twice.
- Every check above, both benchmarks, and the command run once end to end against a throwaway
  Postgres database and demo folder seeded with old grants (dry run, then apply, then a sweep),
  never against the owner's demo data.
- Docs: the M16b upgrade note's manual review replaced by the command (API docs and
  architecture), the threat model, roadmap and both completion records.

Suggested commit: `docs: record verified M21 results`

## Approval and progress record

- Phase 2: design D1–D4 and the name M21 approved by the owner on 2026-11-22.
- Phase 3: this plan, approved by the owner on 2026-11-22.
- S1: built on `old-grant-end-dates`, waiting for owner review.
  - `MockIAM.grant_state` now finds a grant in the 1.0.0 store (M16a to M16b) or only in the
    ledger (M9 to M15) when the 2.0.0 store doesn't have it. Such a grant is `active` until
    revoked, then `revoked`; it never `expired`s by the system's rule, and the system's own
    overdue count still covers 2.0.0 grants only. A new `MockOperations.has` reads whether a key
    was ever accepted, with no visibility delay or injected faults.
  - The it-access bundle registers its IAM as the revoker for `iam.grant_access@1.0.0` too. The
    startup check allows a revoker for a retired write whose tool has a version with an end date,
    and nothing else. Settled here: the dispatch-time set of tools that get an expiry job when
    sent stays current versions only, so nothing changes at dispatch.
  - A test fixture that rebuilds the pre-M16b app now drops both revokers (there were none then).
  - Tests: 8 in `tests/unit/test_old_grants.py` (both kinds of old grant active then revoked; a
    revoke twice recorded once; an unknown key stays absent; a new process sees both; 2.0.0
    unchanged; the startup check; dispatch-time expiry unchanged).
  - `make check` 2498 passed, 77 skipped; scripted 38 of 38 identical to M20's last run;
    integrity 23 of 23.
- S1: approved by the owner and committed.
- S2: built, waiting for owner review.
  - `StepStore.list_for_tool(tool, version)` on memory and Postgres: every step of that version,
    across runs and tenants, by run then step. Settled here: it reads each run's current plan,
    which is every step, since plans only ever have version 1 (automatic replanning was deferred
    in M7). No schema change.
  - `gawe.core.old_grants`: `find_old_grants` takes each retired grant step that was sent
    (`succeeded`, `verification_unknown` or `verification_blocked`) and the attempt that sent it
    (the succeeded one, else the latest). Its idempotency key is the key the target knows the
    grant by: the executor passes the same key to the tool and to the execution record on both
    paths, before M16a and after. The grant's time is that attempt's finish, or its start if it
    never finished. A step with no attempt, a failed, refused or unsent step, and any current
    grant are left out.
  - `end_date`: the later of grant time plus the lifetime and the backfill plus the notice, with
    the side that won; a tie is the natural end. `notice_period` refuses anything outside 1 to 30
    days (7 by default).
  - `AppBundle.grant_lifetime`: the it-access app declares the lifetime it grants today (7 days
    by default); the contract app declares none.
  - Tests: 9 more in `tests/unit/test_old_grants.py` (the rule at both sides and a tie; the notice
    range; grants from both eras with their keys and times; unknown and blocked outcomes; nothing
    GAWE didn't send; tenants apart; the declared lifetime) and a store contract test run on
    memory and Postgres.
  - `make check` 2509 passed, 78 skipped (the new contract test's Postgres case skips without a
    database); `make test-integration` 120 passed; scripted 38 of 38 identical; integrity 23 of 23.
- S2: approved by the owner and committed.
- S3: built, waiting for owner review.
  - `python -m gawe.maintenance end-old-grants [--apply] [--notice-days N]`, on the durable
    stores like `sweep --once` (memory refused, a notice outside 1 to 30 refused, an app with no
    grant lifetime refused; each exits 2). The dry run prints one JSON line per grant (tenant,
    run, step, when made, end date, rule, whether the job exists) and a total; it writes nothing.
    No email, system or level is printed.
  - `--apply` adds M16b's `grant_expiry` job for each grant, due at its end date, with
    `backfill: {rule, notice_days}` in its payload, then audits `grant.end_date_set` (step, grant
    key, end date, rule, notice days) with `once`, and moves the anchor of a finished run past
    it. Settled here: the job id doesn't depend on the date, so a later run reads the existing
    job and keeps the date it was given, and audits from the stored job; a crash between job and
    event is repaired by the next run.
  - Overdue: `GrantExpiryJobs.overdue` adds old-grant jobs past their end date and not done.
    Settled here: they are judged by the end date in the payload, not by when the job is next
    due, because a waiting job is pushed back by the retry interval; the count pages through every
    pending job. A job leased by another sweeper at that moment isn't seen.
  - Not tested separately, as planned: a grant from a cancelled run. The job is M16b's and ignores
    the run's status, which `test_a_cancelled_runs_grant_is_still_revoked` already covers.
  - Tests: 13 in `tests/unit/test_end_old_grants.py`, each on a real 1.0.0 grant sent by the
    pre-M16b app (the dry run; the notice date; apply twice; a crash between job and event; the
    event's fields; the anchor; nothing before the date, then revoked and read back; an ignored
    revoke raising the alarm; a grant already revoked by hand; an unknown outcome that never
    landed; the command's refusals; its output), and one on Postgres (an old-grant job overdue,
    then revoked).
  - `make check` 2521 passed, 79 skipped; `make test-integration` 121 passed; scripted 38 of 38
    identical; integrity 23 of 23.
- S3: approved by the owner and committed.
- S4: cumulative review, negative matrix (6 of 6 caught, control passed), an end-to-end run on a
  throwaway database (a real 1.0.0 grant listed, scheduled once, left alone before its date and
  revoked at it) and final verification. `make check` 2521 passed, 79 skipped;
  `make test-integration` 121 passed; scripted identical; integrity 23 of 23. API docs (a "Grants
  made before M16b" section, the M16b upgrade note), architecture, threat model (T33), roadmap
  (M21 row, queue item 6 done, Next) and the design's build record updated. Logs in
  `bench-results/m21-s4-*.txt`. Waiting for owner review.
- S4: approved by the owner and committed, then merged to `main` by fast-forward.
