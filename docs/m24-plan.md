# M24 build plan

Status: **Done.** S1–S4 approved and committed by the owner and merged to `main` by
fast-forward (2026-11-23). It implements D1–D5 in [m24-design.md](m24-design.md), approved by the
owner on 2026-11-23, without the optional spread rule. No paid model calls.

## Working rules and verification

Work on a branch `combination-rules`. Do not commit, stage or push; the owner reviews and commits
each checkpoint. No live model calls and no real connectors. No migration is planned; if one turns
out to be needed (for example an index on `steps`), it stops for the owner first.

Run S1–S4 in order and stop for owner review after each, with the diff summary, the verification
output (and a log in an ignored `bench-results/m24-sN-*.txt`), known limitations and a suggested
one-line commit message. If verification fails twice on the same step, stop and report. A change
to approved semantics needs a new owner decision; routine details may be settled here and
recorded.

At each checkpoint run `make check` and `make check-docs`, `python -m bench scripted` (identical
records, unless a step moves a case, which is reported first) and `python -m bench integrity`
(23 of 23); `make test-integration` when a step touches code the Postgres tests exercise;
`make ui-check` when the UI changes.

## S1. History, read safely (D1–D3, D5)

- **What an app declares.** `AppBundle.history`: per tool version, the argument that names the
  subject (`contract_id`; the grantee `email`) and how it is compared (exactly; lower-cased), and
  a function that turns the earlier actions into step-scoped variables.
- **`HistoryReader`**, in memory and Postgres: for one tenant, one tool name and one subject, the
  earlier steps that count (D2: every step past policy except `denied`, `rejected` and
  `not_done_by_person`, from this run and earlier ones, never the step being judged), each with
  its run, step, action hash, status, resolved arguments and the run's start time, oldest first.
  The window is applied by the app's function, from the run's start time. Read-only; tenant comes
  from the run, never from the arguments.
- **A lock per subject (D3)** around reading history, evaluating and moving the step to
  `policy_checked`: a thread lock per key in memory, a Postgres advisory lock (on its own
  connection, released in `finally`) otherwise. Key: tenant, tool name and canonical subject.
- **Step-scoped variables.** The orchestrator merges the app's history variables into the
  evaluation context for that step only. A failed read leaves them missing, so the step denies
  (`engine.error`), as any missing variable does.
- **The `history` source kind** in provenance, and a startup check: a rule that reads a variable
  traced to a history source must be `needs_approval` or `deny`. A tool can't write a history
  source (its resource is `gawe.history.<tool>`, and M23 pins that no tool writes `gawe.*`).
- **Audit and approval.** A `history.read` event per judged step with the count and the counted
  steps' references (run, step, action hash). The step's preparation metadata gets a `history`
  entry the app chooses (typed values only), not in the action hash.

No shipped app declares history in S1, so nothing changes in either benchmark.

Tests: the reader per store (what counts and what doesn't, this run and earlier runs, tenant
boundary, subject comparison, ordering); two policy steps for one subject at once, the second
seeing the first in flight; the startup refusal of a history variable in an `allow` rule; an
unreadable history denies; the audit event carries no argument value. A test app in
`tests/api_bundle.py` exercises it end to end. Postgres contract tests for the reader and lock.

Suggested commit: `feat(policy): read an app's own history, for rules that only escalate`

## S2. Contract review: a lowered rating needs legal (D4)

- `crm.write_assessment` declares history on `contract_id`. Variable
  `history.lower_ratings`: the levels below the contract's last counted rating (high > medium >
  low), empty with no earlier rating. Rule `rating-lowered-needs-legal`, `legal_reviewer`.
- The approval view gets a `history` list (tool, the earlier rating, its run and date), and the
  approval card shows it in the typed section. API docs and UI types.

Tests: high then medium in two runs waits, then runs once approved; medium then high, a first
rating and a repeated `high` run automatically; a rating in another tenant doesn't count; a
denied or rejected earlier rating doesn't count; an earlier rating still waiting does; two
assessments in one plan; golden cases; the adversarial oracle and boundary registry. Report
any scripted case that moves.

Suggested commit: `feat(contract-review): a rating lower than the contract's last needs legal`

## S3. IT access: repeated grants need security (D4)

- `iam.grant_access` declares history on `email`, lower-cased. Tenant settings
  `automatic_grants` (2) and `history_days` (30). Variable `history.repeated_systems`: systems
  this grantee has had granted `automatic_grants` times within `history_days`. Rule
  `repeated-grant-needs-security`, `security_admin`.
- The approval card shows the earlier grants (system, level, date).
- M15's sequence tripwire extended: random sequences of allowed actions, then a repeat; the
  repeat must wait.

Tests: two grants of `wiki` run, the third waits and runs once approved; a grant older than the
window doesn't count; another system doesn't count; a different grantee doesn't; case variants
of one address count as one; a missing setting denies; the concurrency test with two runs at
once; the adversarial suite (oracle, boundaries, unreadable targets); the clean set (report any
move first).

Suggested commit: `feat(it-access): a grant asked for again and again needs security`

## S4. Verification and completion record

- Review the cumulative diff for scope, compatibility and privacy.
- Negative matrix on exported copies, each with a test that fails when the guard is removed, after
  a control run: a history variable allowed in an `allow` rule; the reader skipping in-flight
  steps; the lock removed; the reader not scoped to the tenant; the grantee not lower-cased.
- Every check above and both benchmarks.
- Docs: threat model (a combinations row), API docs (settings, history on the approval card, the
  audit event), roadmap (M24 row, queue item 9 done), both completion records.

Suggested commit: `docs: record verified M24 results`

## Approval and progress record

- Phase 2: design D1–D5 and the name M24 approved by the owner on 2026-11-23, without the
  optional spread rule.
- Phase 3: this plan, approved by the owner on 2026-11-23.
- S1: built on `combination-rules`, reviewed and committed by the owner.
  - `gawe.core.history`: `HistorySubject` (the argument naming the subject, lower-cased or
    exact, and the app's functions for variables and for what the approver sees), the readers
    (`InMemoryHistoryReader`; `PostgresHistoryReader` in `gawe.db.history_store`), the subject
    locks (a thread lock per key; a Postgres session advisory lock on its own connection) and
    `History`, which joins them. `AppBundle.history` and `Stores.history` / `Stores.locks`.
  - The orchestrator holds the subject's lock while it reads history, evaluates and stores the
    decision; the variables go in under `history` for that step only, the matched history into
    the step's preparation metadata as `history`, and a `history.read` event records the count
    and the counted steps' run, step and action hash (or the error class when unreadable, which
    leaves the variables out, so the step denies).
  - Startup: a rule reading a history variable must not be `allow` (in provenance, through the
    new `history` source kind); a `history.*` variable must depend on history sources only; a
    history source must be `gawe.history.<tool>` of a declared tool; a declared tool must exist
    and have the subject argument; stores without a reader refuse an app that reads history.
  - Settled here: history is keyed by tool name, across its versions, not per version. The window
    is applied by the app's function from each run's start time, which both stores have (a step's
    own time isn't kept in memory and changes on every status in Postgres). In the same run only
    steps earlier in the plan count.
  - Found while building and settled here: the daily review re-runs policy on earlier automatic
    decisions with today's values. With history it would also count actions taken after the one
    it checks, and flag it as changed. It now reads history as of the judged run's start.
  - Settled here: the Postgres reader selects the tenant's counted steps of the tool and compares
    the subject in Python, the same comparison the declaration makes; a large tenant would want an
    index on `steps`, which needs a migration and the owner first.
  - Tests: 26 in `tests/unit/test_history.py` (a test app whose second assessment of a contract
    waits for legal: across runs, within a run, another tenant not counted, what the approver
    sees, the audit references only, unreadable history denies, two runs at once — which fails
    with the lock removed — what counts by status, any version and lower-casing, `as_of`, and the
    startup refusals) and 4 Postgres tests in `tests/contract/test_history_postgres.py`.
  - `make check` 2653 passed, 84 skipped; `make test-integration` 126 passed; scripted 38 of 38
    identical; integrity 23 of 23.
- S2: built, reviewed and committed by the owner.
  - Contract Review declares history on `crm.write_assessment` keyed on `contract_id`, a
    `history` source `earlier_ratings`, the variable `history.lower_ratings`, and the rule
    `rating-lowered-needs-legal` (`legal_reviewer`).
  - Found while building and settled here: "the last rating" can't simply be the latest counted
    one. After a "high" that took effect, a "medium" waiting for legal would make a second
    "medium" look like no change, and if legal rejected the first, the contract would drop from
    high with no person. The variable compares with the highest of the last rating that took
    effect (`succeeded`, `done_by_person`) and every counted rating after it. An approved
    downgrade still lowers the bar for later runs.
  - What the approver sees is now declared as argument names (`HistorySubject.shown`), each
    checked at startup to be a typed field of every current version, instead of a function, so
    no model-written text reaches the card this way. Items carry the tool, run, step, the run's
    start and those values; the card shows the latest 10 and the audit event counts them all.
    This run's earlier steps come last, in plan order.
  - The approval view gets `history` (`HistoryItemView`), shown on the card under "Earlier
    actions on the same subject" with a link to each run. API docs and UI types updated.
  - The sequence tripwire moved, not the scripted benchmark: a prefix rating C-1001 "medium"
    makes the honest "low" that follows also match the new rule, and the harness's honest
    approver accepted only the every-low rule beside the baseline. It now accepts the new rule
    too; it still refuses the attack step and a fooled Reader. Scripted cases start with no
    history, so none can match it. One daily review test rated C-1001 "medium" and then "low"
    without the every-low rule; its "medium" now rates another contract.
  - Tests: in `tests/unit/test_apps_contract_review.py`, high then medium in two runs waits and
    runs once approved, with the earlier rating on the card; raised, repeated, first and
    another contract's ratings run; another tenant's doesn't count; a rejected one doesn't; a
    waiting one never hides the one before (fails with "latest" in place of the standing
    rating); two in one plan; and the variable's table. The boundary registry and generator
    gain the rule with the earlier rating written by hand; the unreadable suite gains the
    variable as a target. Tests that call the engine directly now pass no earlier rating.
  - `make check` 2715 passed, 84 skipped; `make ui-check` 40 passed; `make test-integration` 126
    passed; check-docs 0 problems; scripted identical to S1; integrity 23 of 23.
  - Limit: order is by run start, not by when a write took effect. Two runs on one contract whose
    writes land out of start order could leave the standing rating misread; the subject lock
    orders their policy checks, not their writes.
- S3: built, reviewed and committed by the owner.
  - IT access declares history on `iam.grant_access` keyed on `email`, lower-cased, showing
    `system` and `level`. Tenant settings `automatic_grants` (2) and `history_days` (30). The
    variable `history.repeated_systems` lists the systems this person has been granted
    `automatic_grants` times or more, any level, in the `history_days` before the judged run
    started; a missing or unusable setting gives no variable, so every grant denies. The rule
    `repeated-grant-needs-security` (`security_admin`).
  - Settled here: a `history.*` variable may also read tenant settings, since the window and
    count are settings; it must still read a history source, and findings or target resources
    are refused. Startup check and its tests changed to match.
  - Settled here: cancelling a run fails its waiting step, and a failed step counts (D2), so a
    cancelled grant still counts toward repeats. That only adds reviews.
  - The sequence tripwire moved, not the scripted benchmark or the clean set (every trial starts
    with empty stores): generated prefixes could grant one system three times, and two wiki
    grants in a prefix made the attack run's honest wiki grant the third. The prefix tests keep
    checking the other rules with repeats allowed; a new tripwire plays random sequences of
    allowed grants, each in its own run and approved or rejected at random, against plain
    counting: once a system has had two counted grants the next waits, the card lists those
    earlier grants, and a rejected one doesn't count.
  - Tests: in `tests/unit/test_apps_it_access.py`, two wiki grants run and the third waits, shows
    both earlier grants and runs once approved; grants older than the window don't count;
    another system or grantee doesn't; case variants of one address are one person; a missing
    setting denies; two runs at once after one grant end one done and one waiting (fails with the
    lock removed). The boundary registry and grant generator gain the rule, with plain-arithmetic
    tests of the window edge, the count and unusable settings; the unreadable suite gains the
    variable as a target. Tests that call the engine directly pass no earlier grants.
  - `make check` 2875 passed, 84 skipped; `make test-integration` 126 passed; check-docs 0
    problems; scripted identical to S1; integrity 23 of 23.
  - Limit: an upper-cased address is already denied by the requester binding (M15), so case
    variants are tested at the history level, not through a full run.
- S4: verified; approved by the owner and committed. Cumulative diff reviewed; negative matrix 5 of 5
  caught after a passing control run (log in `bench-results/m24-s4-matrix.txt`); every check and
  both benchmarks pass (`bench-results/m24-s4-check.txt`). Docs: threat model T43, API docs (rules
  on earlier actions: the settings, the approval view's `history`, the `history.read` event),
  roadmap (M24 row, queue item 9 done, the M15 row, known limits), and the build record in
  [m24-design.md](m24-design.md).
