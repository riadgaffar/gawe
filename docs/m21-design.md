# M21 design: An end date for grants made before M16b

Status: **Done.** S1–S4 approved and committed by the owner and merged to `main` by fast-forward
(2026-11-22). D1–D4 approved as drafted on 2026-11-22. No paid runs. The plan and its progress
record are in [m21-plan.md](m21-plan.md); results are in the build record at the end.

## 1. Purpose and boundaries

Since M16b every access grant has an end date: the IAM system treats it as inactive from then, and
a `grant_expiry` job revokes it. Grants made before that, by `iam.grant_access@1.0.0`, have no end
date. They never expire, aren't counted as overdue, and the upgrade notes tell the operator to
review them by hand. Nothing checks that anyone did.

M21 gives each of those grants an end date and lets the existing expiry job end it. The open
question, queued since M16b, is what end date is fair.

In scope: finding the old grants from GAWE's own records, choosing their end dates, scheduling and
auditing their expiry, the mock IAM's support for revoking them, and the operator command. Out of
scope: grants GAWE didn't make, a real IAM connector, notifying the people who hold the grants,
and any change to 2.0.0 grants.

## 2. What the codebase does today

- **Two kinds of old grant.** From M9 to M15 the mock IAM recorded a grant only in its ledger
  (`iam.grant`, keyed by the idempotency key). M16a added a separate store for 1.0.0 grants
  (`iam-system.sqlite3`). M16b moved new grants to 2.0.0 and a third store, with an end date per
  grant and a revocation table. Old grants are in the first two places, with no field for an end
  date.
- **GAWE knows every grant it made.** The `executions` table has existed since M3. For each
  attempt it keeps the idempotency key, the status and when it finished. A succeeded 1.0.0 grant
  step therefore gives the key the target knows the grant by and the time it was made, without
  reading the target.
- **Expiry is a maintenance job.** `grant_expiry` is due at the end date, revokes by key with its
  own operation key, and finishes only when the IAM system reads the grant back as revoked or as
  never granted. It narrows access only, so it needs no policy decision or approval; it's audited
  on the grant's run (`grant.revoked`). An overdue alarm fires when a grant is past its end date
  and nothing revoked it.
- **The mock can't revoke an old grant yet.** Its revocation table takes any key, but its grant
  state reads only the 2.0.0 store, so an old grant reads as "absent" and a job would finish
  without ending it.
- **The tenant's grant lifetime** is a constant per app (7 days by default, 1 hour to 30 days);
  there is no per-tenant store.

## 3. Decisions for the owner

### D1. What end date is fair

| Option | End date | Problem |
|---|---|---|
| **A. As if made today's way, with notice (recommended)** | Grant time plus the tenant's lifetime, but never earlier than the backfill plus a notice period (7 days by default, 1 to 30) | Close to B in practice: every old grant is more than a week old, so with today's 7-day lifetime nearly all land on the notice date |
| B. One date for all | The backfill plus the notice period | Simple, but a grant made yesterday under 1.0.0 gets the same date as one from October |
| C. As if made today's way, no notice | Grant time plus the lifetime | Almost every old grant is already past it, so all are revoked at the next sweep, with no warning |
| D. No backfill | Keep the manual review, but list the grants | The gap stays; it just becomes visible |

Recommendation: A. No old grant outlasts what a new grant made the same day would get, except by
the notice, and nobody loses access without warning. If the lifetime ever grows, recent old
grants get their natural end instead of the notice date.

### D2. Where the end date lives and how it's enforced

| Option | How | Problem |
|---|---|---|
| **A. A GAWE expiry job per old grant (recommended)** | The backfill schedules M16b's `grant_expiry` job for each old grant, due at its end date. The job revokes through the IAM system and waits to read it back as revoked. The overdue count includes old grants past their date that nothing revoked, read from these jobs | The IAM system itself still doesn't know the end date: if the job stalls, the grant stays active (the overdue alarm says so) |
| B. Write the end date into the IAM record | The system enforces the date itself, as for 2.0.0 grants | A real IAM system may not allow changing an existing grant; it works only because the mock is ours |

Recommendation: A. It's the path M16b already tests, and the alarm covers the case B would.

### D3. How the backfill runs

| Option | How | Problem |
|---|---|---|
| **A. An operator command, dry run by default (recommended)** | `python -m gawe.maintenance end-old-grants` lists how many old grants there are, their run ids and their end dates, and changes nothing; `--apply` schedules the jobs and audits `grant.end_date_set` on each grant's run, with the date and the rule that set it. Job ids are deterministic, so running it again adds nothing | Someone has to run it |
| B. Automatically at startup or in a migration | Nobody can forget | Access starts ending on deploy, which surprises people. Declined |

Recommendation: A. The dry run never shows email addresses or systems, only run ids, counts and
dates, the same rule as the audit trail.

### D4. Which grants count

| Option | Grants | Problem |
|---|---|---|
| **A. Every 1.0.0 grant GAWE sent (recommended)** | Steps of `iam.grant_access@1.0.0` with a succeeded execution, plus any whose outcome was left unknown or blocked. The job revokes only what the IAM system reads as active, so a grant that never landed, or was already removed by hand, ends as "nothing to revoke" | None found |
| B. Every grant in the IAM system's old stores | Also catches grants GAWE didn't make | GAWE would revoke access it never granted; that's an IAM clean-up, not GAWE's job |

Recommendation: A.

## 4. What changes, if approved

- Mock IAM: grant state and revocation for both kinds of old grant (the ledger and the 1.0.0
  store), and old grants counted as overdue when their GAWE job is past due.
- Maintenance: the `end-old-grants` command (dry run and apply), reusing `grant_expiry`; the job's
  end date comes from the backfill instead of the step's arguments.
- Audit: `grant.end_date_set` (run, step, end date, which rule set it), then the usual
  `grant.revoked`.
- Docs: the M16b upgrade note's manual review replaced by the command, API docs, threat model,
  roadmap.
- Tests: end dates under each rule; idempotent re-runs; both kinds of old grant revoked and read
  back; a grant already gone; an unknown outcome; a stalled revoke raising the overdue alarm; the
  dry run changing nothing and printing no personal data.

## 5. Expected changes to existing numbers

None. The benchmarks use 2.0.0 grants only, so the scripted run and the integrity suite stay as
they are.

## 6. Limits

- The mock is the only IAM system; a real connector needs a revoke call keyed by GAWE's key or by
  the grant id GAWE saved.
- Nobody is told their access is ending; the operator gets the list from the dry run.
- There is no way to keep an old grant past its date. As for 2.0.0 grants, renewal is a new
  request.
- The lifetime and notice are per app until there is a tenant store.

## 7. Approval

Approved by the owner on 2026-11-22: D1–D4 as drafted, the name M21, and no paid runs. The build
plan is in `docs/m21-plan.md`.

## Build record

| Decision | Proposed | Result |
|---|---|---|
| D1 | The later of grant time plus today's lifetime and the backfill plus a notice | Done: `end_date` names which side won; a tie is the natural end; the notice is 1 to 30 days, 7 by default; the IT app declares its lifetime |
| D2 | A GAWE expiry job per old grant, revoked through the IAM, counted as overdue from the jobs | Done: M16b's `grant_expiry` job with a `backfill` record; the mock IAM reads and revokes both kinds of old grant; overdue is judged by the end date, not by when the job is next due |
| D3 | An operator command, dry run by default | Done: `python -m gawe.maintenance end-old-grants [--apply] [--notice-days N]`; ids, dates and the rule only; a second run keeps each grant's first date and writes nothing new |
| D4 | Only 1.0.0 grants GAWE sent | Done: from GAWE's steps and executions, including outcomes left unknown or blocked; the execution's key is the key the IAM system holds, before and after M16a |

### S4. Verification

Review of the cumulative diff (18 files): the mock IAM's old-grant reads, the 1.0.0 revoker and
its startup check, the step store query, the finder and end-date rule, the command, the overdue
count, tests and docs. Nothing changes until an operator applies the command; the set of tools
that get an expiry job at dispatch is unchanged. Output, logs and audit carry ids, dates, the rule
and the grant key, never the grant's email, system or level.

Negative matrix on exported copies, after a control run that passed: 6 of 6 caught.

| Mutation | Caught by |
|---|---|
| The dry run writes jobs | `test_the_dry_run_lists_the_grant_and_writes_nothing`, the command's output test |
| The notice ignored | the end-date rule test, `test_an_older_grant_gets_the_notice_date`, the apply test |
| Old grants read only from the current store | both old-grant state tests and the new-process test |
| Old-grant jobs left out of the overdue count | `test_a_revoke_the_system_ignores_stays_waiting_and_raises_the_alarm` |
| A failed step counted as a grant | `test_nothing_gawe_did_not_send_is_counted` |
| The backfill audits every run | the apply-twice, crash and anchor tests |

End to end on a throwaway Postgres database and demo folder, seeded with the dev tenant: the
pre-M16b app sent AR-2041's wiki grant as 1.0.0, then the current app read it back. The dry run
listed it and wrote nothing; apply scheduled it (rule `notice`, 7 days); a second apply with
20 days' notice kept the first date and added nothing; a sweep at once revoked nothing and
reported no overdue; a sweep at the end date revoked it, the IAM read it back as revoked, and the
run's chain verified with one `grant.end_date_set` and one `grant.revoked`. The database was
dropped afterwards.

```text
make check: ruff clean; mypy clean; 2521 passed, 79 skipped
make test-integration (Postgres): 121 passed
make check-docs: checked 203 references, 0 problem(s)
python -m bench scripted: 38 records, identical to M20's last run
python -m bench integrity: 23/23 match
make test-schema, alembic, make ui-check: not run; no schema or UI change
```

Limits that stand:

- The IAM system doesn't know an old grant's end date; only its GAWE job ends it. A grant whose
  job is missing is never counted as overdue.
- Nobody is told their access is ending; the operator has the dry run's list.
- There is no way to keep an old grant past its date; renewal is a new request.
- The mock is the only IAM system; a real connector needs a revoke keyed by GAWE's key.

