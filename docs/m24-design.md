# M24 design: Combination rules within one app

Status: **Done.** S1–S4 approved and committed by the owner and merged to `main` by fast-forward
(2026-11-23). Approved 2026-11-23 (D1–D5 as drafted, without the optional spread rule). No paid
runs.

## 1. Purpose and boundaries

Queue item 9, split out of M15. Every rule today judges one action alone. An attacker's sequence
shows only in combination: each action passes, the series doesn't. M15 set the principle, from a
reader: history may only **escalate**, adding a review and never removing one, and it is read
from records the engine writes and no tool can, never from what the agent's own tools wrote.

In scope: history across runs and within a run, in one app and one tenant; a rule per shipped app;
the load-time checks that keep history escalation-only; tests. Out of scope: combinations across
apps (they still need a subject key both apps share, from a system of record), and the held
record-diff item.

## 2. What the codebase does today

**Every combination below goes through with no person** (probe, not in the repo):

| Sequence | Today |
|---|---|
| Contract C-1001 rated `high`, then `medium` in a later run | both automatic. Since M17 every `low` waits for legal, but a drop from `high` to `medium` doesn't |
| Dana files four portal tickets for `wiki` write, one after another | four automatic grants. Each expires after 7 days (M16b), but asking again each week keeps the access with nobody looking |
| Then `billing` write and `crm` read | automatic; nothing notices one person collecting access |

Other relevant facts:

- **Where history already lives.** Each step record holds its tool, version, resolved arguments
  (the contract id and rating; the grantee, system and level), status and run, and the run holds
  the tenant. Only the engine writes step records; tools can't (M23 pins that no tool writes a
  `gawe.*` resource), and each step's signed decision is bound to the hash of exactly those
  arguments (M23's trace checks it). Postgres keeps them in `steps` (`tool_name`, `resolved_args`
  as jsonb, `updated_at`). Every past step is there, so history starts complete, not unknown.
- **The audit trail stores payload hashes only.** M15's sketch therefore needed subject digests
  under a new HMAC key, a projection, key rotation and an event-format change. Reading the step
  records instead needs none of that: the subject is already in the arguments, in the same
  tenant-protected store.
- **Variables are per run.** `bundle.variables(run)` sees the run, not the step being judged, so
  no rule can ask "this contract's last rating". A missing variable denies (`engine.error`).
- **Strictest wins.** deny > needs_approval > allow, and matching rules naming different approver
  roles deny. A needs_approval rule can only add a review.
- **M22's rule on running totals.** A write judged by a running total needs atomic reservation at
  its target. A count of earlier actions is a running total kept by GAWE itself, so the same
  question applies: two runs at once must not both see "none before".

## 3. Decisions for the owner

### D1. Where history comes from

| Option | How | Problem |
|---|---|---|
| **A. GAWE's own step records (recommended)** | A read-only, tenant-scoped history reader over the step and run stores: steps of a tool version whose arguments name the same subject | History lasts as long as step records do (nothing deletes them today) |
| B. M15's sketch: keyed subject digests in the audit trail | HMAC key, key id, rotation, an indexed projection bound to audit events, a new event format | Much larger; needed only if history must outlive step records or be shared across apps |
| C. The target's records (the CRM's last rating) | | Tools write them; rejected in M15 |

### D2. What counts as an earlier action

| Option | Counts | Problem |
|---|---|---|
| **A. Anything that may have taken effect or still may (recommended)** | Every step past policy except `denied`, `rejected` and `not_done_by_person`, in this run or earlier ones. In-flight, waiting and unknown steps count | A failed write counts though it may never have happened: more reviews, never fewer |
| B. Only `succeeded` and `done_by_person` | | Two runs in flight at once both see nothing; an unknown outcome is treated as absent |

### D3. Two runs at the same time

| Option | How | Problem |
|---|---|---|
| **A. A lock per subject around the policy step (recommended)** | Reading history and moving the step to `policy_checked` happen under a lock on (tenant, tool, subject): a thread lock in memory, a Postgres advisory lock otherwise. With D2-A the in-flight step is the reservation | One more store method, and a Postgres lock to test |
| B. Name the window, as M22 did | | Milliseconds, but M22's own rule says a running total needs a reservation |

### D4. The rules shipped

| App | Rule | Setting |
|---|---|---|
| Contract review | `rating-lowered-needs-legal`: a rating lower than the contract's last counted rating needs `legal_reviewer` (high→medium is new; anything→low already waits) | none; order high > medium > low |
| IT access | `repeated-grant-needs-security`: once a person has had a system granted the allowed number of times within the window, the next grant of it needs `security_admin` | `tenant.automatic_grants` (proposed 2: the first grant and one renewal) and `tenant.history_days` (proposed 30) |

Not shipped: a rule on how many systems one person can write to. Today only `wiki` and `billing`
allow automatic write (`crm` waits since M23, `prod_db` at any level), so a third system always
waits already; the repeat rule covers asking again for the same grant. It becomes worth adding
when the IT app gains more systems that allow automatic write. The settings are per tenant, so each
deployment can choose its own.

A first rating, or a first grant, has no history and is judged as today.

### D5. How rules read history, and keeping it escalation-only

- **Step-scoped variables.** The app's variables function also receives the step's tool and
  resolved arguments and the history reader, and computes plain lists: for contract review,
  `history.lower_ratings` (the levels below the contract's last rating); for IT,
  `history.repeated_systems` (systems this grantee already had granted the allowed number of times in the window).
  Rules stay declarative: `risk_level: {in: $history.lower_ratings}`.
- **A new source kind, `history`.** Provenance traces every variable to it. Startup refuses a
  history variable in an `allow` rule, so history can only add a review or a denial. Unreadable
  history denies, as any missing variable does today.
- **Subjects are canonical.** Emails compared lower-cased, contract ids exactly as the contract
  system's preparation bound them.
- **What the approver sees.** The step's preparation metadata names the history that matched
  (the earlier rating and its date; the earlier grants' dates), shown as typed values on the
  approval card. Not in the action hash: history only grows, so a later change can only add
  reviews. The audit trail gets a `history.read` event with the count and the step references
  counted (run, step, action hash), never argument values.

## 4. What changes, if approved

- Core: a `HistoryReader` over the step and run stores (memory and Postgres), the lock (D3),
  step-scoped variables, the `history` source kind and its escalation-only startup check.
- Apps: contract review's `rating-lowered-needs-legal`; IT's `repeated-grant-needs-security`,
  with its tenant settings.
- The adversarial suite: oracle and boundary entries for the new rules; a sequence test (M15's
  drift tripwire) extended so a run of allowed actions can no longer reach an escalation-free
  repeat.
- Docs: threat model (a combinations row), API docs (settings, approval-card history), roadmap,
  completion records. No schema change is expected; an index on `steps (tool_name)` may be worth
  adding, and would stop for the owner first.

## 5. Expected changes to existing numbers

Benchmark trials use fresh stores, so history across runs never shows there. Within one run, a
case that plans two assessments of one contract, or two grants of one system, could move from
"ran" to "waits"; any such move is reported as a finding first. The integrity suite should stay
at 23 of 23.

## 6. Limits

- History covers one app in one tenant; combinations across apps still wait for a shared key.
- History lasts as long as step records. A retention policy that deletes them would shorten it;
  none exists, and one would need to keep what the rules read.
- Counting failed and unknown writes adds reviews an honest user didn't strictly need.
- A determined user can wait out the window; the window is a tenant setting.
- No rule limits how many systems one person can write to (see D4); with today's systems it would
  change nothing.

## 7. Approval

Approved by the owner on 2026-11-23: D1–D5 as drafted, without the optional spread rule, the name
M24, and no paid runs. The build plan is in `docs/m24-plan.md`, approved the same day.

## Build record

| Decision | Proposed | Result |
|---|---|---|
| D1 | History from GAWE's own step records | Done: `gawe.core.history` with an in-memory and a Postgres reader. Keyed by tool name across versions, in one tenant; no keyed hash or key management |
| D2 | Count everything past policy but denied, rejected and not done | Done. In-flight, failed and unknown steps count; so does a cancelled waiting step, which the engine marks failed. In the same run only earlier plan steps count |
| D3 | A lock per subject | Done: a thread lock in memory, a Postgres advisory lock otherwise, held while history is read, policy evaluated and the decision stored |
| D4 | One rule per app | Done: `rating-lowered-needs-legal` and `repeated-grant-needs-security` (`automatic_grants` 2, `history_days` 30). No spread rule |
| D5 | Escalation only, shown and audited | Done: a `history` source kind; startup refuses a history variable in an `allow` rule and one that reads anything but history and tenant settings; the approval view's `history`; the `history.read` event by reference |

Found while building and fixed:

- The daily review re-runs policy on earlier automatic decisions. With history it would have
  counted actions taken after the one it judges; it now reads history as of the judged run's
  start.
- "The last rating" can't be the latest counted one: a "medium" waiting after a "high" would
  have made a second "medium" look like no change, and if legal rejected the first, the contract
  would have dropped from high with no person. The rule compares with the standing rating, the
  highest of the last one that took effect and every counted one after it.

Settled during the build: what the approver sees is declared as argument names, each checked at
startup to be a typed field; a history variable may read tenant settings too (the window and the
count). As expected in section 5, no benchmark number moved: every trial starts with empty stores.
The sequence tripwire did move. Its prefix tests now allow repeats, and a new tripwire covers
repeated grants. The bench's honest approver also accepts `rating-lowered-needs-legal` beside
the every-low rule.

### S4. Verification

Review of the cumulative diff (31 files): the history module and Postgres store, provenance's
history kind, wiring checks, the orchestrator's lock and audit event, the daily review's as-of
read, both apps' declarations and rules, the approval view and card, tests and docs. Every new
`AppBundle` and `Stores` field has a default; an app that declares history refuses to start on
stores without a reader (fail closed). The audit event carries references only; the approval
view and hand-off view carry earlier actions' typed values, to people in the same tenant.

Negative matrix on exported copies of `HEAD`, after a control run that passed: 5 of 5 caught.

| Mutation | Caught by |
|---|---|
| An `allow` rule may read history | `test_an_allow_rule_reading_history_is_refused` |
| The reader skips in-flight steps | `test_two_runs_at_once_dont_both_see_one_grant_before` |
| The subject lock removed | both two-runs-at-once tests (history core and IT access) |
| The reader not scoped to the tenant | `test_another_tenants_history_doesnt_count`, `test_another_tenants_rating_doesnt_count`, the reader's subject test |
| The grantee's address not lower-cased | `test_case_variants_of_one_address_are_one_person` |

```text
make check: ruff clean; mypy clean; 2875 passed, 84 skipped
make test-integration (Postgres): 126 passed
make ui-check: 40 tests, build
make check-docs: checked 243 references, 0 problem(s)
python -m bench scripted: identical to S1's run (and M23's)
python -m bench integrity: 23/23 match
make test-schema, alembic: not run; no schema change
```

Limits that stand:

- One app and one tenant; combinations across apps still need a shared subject key.
- Earlier actions are ordered by run start, not by when a write landed.
- Counting failed, unknown and cancelled actions adds reviews an honest user didn't strictly
  need. A patient user can wait out the window.
- The Postgres reader loads a tenant's counted steps of the tool before comparing subjects; a
  large tenant would want an index on `steps`, which needs a migration and the owner first.
- An upper-cased address is denied by M15's requester binding, so case variants are tested at the
  history level, not through a full run.
