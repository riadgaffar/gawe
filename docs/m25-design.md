# M25 design: Replay of failed events, scoped to a tenant

Status: **Done.** S1–S5 approved and committed by the owner and merged to `main` by fast-forward
(2026-11-23). Approved 2026-11-23 (D1–D5 as drafted, each with the recommended option). No paid
runs.

## 1. Purpose and boundaries

Queue item 7. Four r/LLMDevs readers asked for the same thing from different angles: an agent
that replays dead-lettered events (failed webhook deliveries, billing events) must not widen its
scope, cross tenants, bill twice or act on events that changed after someone looked. What they
asked for, in our words:

- **Freeze the batch at preview.** Exact event ids, versions, amounts and destinations are fixed
  before anyone authorizes, inside the hash. Execution uses that saved set, never a fresh query
  such as "failed in the last 24 hours".
- **The tenant comes from the event record**, not from anything the model wrote.
- **Reuse the original effect's identity, or reconcile it.** A replay under a fresh key can bill
  twice when the original effect landed late (a "late callback"). Missing evidence stays
  unknown, not failed.
- **Cap the amount**, and say what the cap is checked against: numeric rules alone don't make an
  amount trustworthy or enforce a budget across batches.
- **The reader's test:** preview A and B, change B, add a newly failed C. C was never
  authorized; B needs a new preview; whether A still runs is a decision (D2).

In scope: a third shipped app over a mock dead-letter queue and a mock billing system, its rules,
tests, scripted attack cases and docs. Out of scope: real queue or billing connectors; the
held record-diff item.

## 2. What the codebase does today

- **Probe (not in the repo):** an IT grant whose reply is lost is confirmed by read-back, and the
  run completes (M16a). Asking for the same grant again in a new run makes a second grant: the new
  run has a new action hash, so a new idempotency key. GAWE has no notion of replaying an earlier
  effect under its original identity.
- **Idempotency keys are GAWE's.** The executor derives each step's key from its action hash, and
  the mocks deduplicate on that key only. A replayed event needs the target to deduplicate on the
  event's own delivery key.
- **Most of what replay needs exists.**
  - Preparation fills trusted values from the system of record and binds record revisions (M16b).
  - Targets do conditional writes in one transaction and are read back by key (M16a/b).
  - Connectors declare guarantees, and a write that relies on a running total needs
    `atomic_reservation` (M22). That guarantee is declared but no shipped write uses it yet.
  - Policy has `gt`/`lte` and a `counter` source kind.
  - Record binding is the app's own schema, so a batch can bind one revision per event without an
    engine change.
- **An app can run without a Reader:** the Planner works from the run's objective.
- **One app per deployment** (`GAWE_APP`). A third app is its own deployment.

## 3. Decisions for the owner

### D1. What "replay" means here

| Option | What | Assessment |
|---|---|---|
| **A. A third app: replay dead-lettered events (recommended)** | An operator asks, say, "replay the failed `invoice.paid` events for customer X since the outage". The agent lists them; GAWE freezes, judges and sends the batch | What the readers described, and a second domain where money moves |
| B. Replay GAWE's own failed steps | An operator command that re-sends a failed step under its original key | Little to gain: a lost reply is already reconciled by read-back (probe), and a step that failed before dispatch did nothing, so a new run is right |
| C. Both | | B adds a surface for little gain |

### D2. Batch semantics (the reader's A/B/C test)

| Option | What happens when B changed after preview | Cost |
|---|---|---|
| **A. All or nothing (recommended)** | The target checks every event's revision and the caps, and applies the whole batch in one transaction. B changed, so nothing is applied (`event_changed`); a new preview covers A and B again. C is never in it | One effect per step, one approval, one read-back. A real target must support a batched conditional write, or the app makes the action prepare-only (M22) |
| B. Per-event outcomes in one step | A runs, B is refused | A step with partial success: new statuses, partial read-back, a muddier approval |
| C. One step per event | A runs, B's step is refused | 50 events mean 50 approvals |

### D3. Effect identity and late callbacks

- Each event is delivered under **its original delivery key**, taken from the event record. The
  billing system deduplicates on it, so a replay can't bill twice.
- The batch as a whole is idempotent under GAWE's key, as every write is today.
- **At preview**, preparation reads the billing system by each event's key. An event already
  delivered (its callback just arrived late) is dropped from the batch and listed as
  `already_delivered`: it needs no replay.
- **At the write**, an event delivered between preview and write is a no-op inside the batch and
  reported as such. Repeating it would change nothing, so it doesn't refuse the batch.
- Read-back confirms the batch by GAWE's key and each event by its own key. Missing evidence stays
  unknown (M16a).

### D4. Scope and caps

- **Facts come from records, never from model arguments.** The tenant, customer, amount and
  destination come from the event record. An event of another tenant fails preparation
  (`event_other_tenant`), as does one no longer dead-lettered (`event_not_failed`). The list
  tool's credential is scoped to the run's tenant.
- **Per-batch rules** judge the prepared count and total:
  - up to 10 events and 1,000 run automatically;
  - up to 100 events and 25,000 need a `finance_approver`;
  - above that, denied;
  - a batch spanning more than one customer needs finance.

  The numbers are proposals.
- **A daily cap per tenant.** Splitting one large replay into many small batches would pass every
  per-batch rule.

| Option | What | Assessment |
|---|---|---|
| **A. Daily cap with atomic reservation (recommended)** | Tenant setting `daily_replay_limit` (proposed 50,000). The billing system reserves each batch's total against the day's running total in the same transaction as the write; over the limit, nothing is applied | What the readers meant by a dollar cap, and the first real use of M22's reservation guarantee |
| B. Per-batch caps only | | Leaves the split open; M24-style history could count batches, but money needs a total, not a count |

### D5. What the model sees

The list tool returns each event's id, type, customer, amount, failure time and status, never its
payload body. Payloads are written by whoever sent the webhook, so they are attacker text. The
Planner turns the operator's request into the list tool's filters (customer, type, time window)
and passes the listed ids to the replay step. Preparation then freezes what each id means. A plan
that widens the filters still meets the count, total, customer and daily rules.

## 4. What changes, if approved

- **App:** `gawe.apps.event_replay` with tools `events.list_failed` (read) and
  `billing.replay_events` (write, prepared, record-bound), and its preparation, read-back,
  guarantees, credentials, rules, settings and approval-card fields (each event's id, customer
  and amount, typed).
- **Mocks:** a dead-letter queue and a billing system that deduplicates on delivery keys, applies
  a batch in one guarded transaction and reserves against the daily total.
- **Engine:** none expected. If one turns out to be needed, it comes to the owner first.
- **Tests:**
  - the reader's A/B/C test;
  - a late callback;
  - a lost reply;
  - another tenant;
  - widened filters;
  - exact-limit and over-limit controls;
  - two batches racing the daily cap;
  - a payload carrying injected text;
  - the adversarial oracle and boundary registry.
- **Benchmarks:** replay attack cases (cross-tenant, widened scope, over the cap, injected
  payload) and integrity scenarios (late callback, lost reply), reported as their own group so
  the existing 38 scripted records and 23 integrity scenarios stay identical.
- **Docs:** threat model, API docs, roadmap, completion records. Credit the readers.

## 5. Expected changes to existing numbers

None: a new app, with its cases reported separately.

## 6. Limits

- Mocks only. A real dead-letter queue or billing system must support batched conditional writes,
  deduplication on the original delivery key and reservation, or the replay is prepare-only.
- The caps are proposals, not measured.
- The agent adds little over a well-built replay script. Its value here is showing that a model
  in the loop can't widen, cross or repeat what a person approved.

## 7. Approval

Approved by the owner on 2026-11-23: D1–D5 as drafted, each with the recommended option, the
name M25, and no paid runs. The build plan is in `docs/m25-plan.md`, approved the same day.

## Build record

| Decision | Proposed | Result |
|---|---|---|
| D1 | A third app over a mock dead-letter queue and billing system | Done: `gawe.apps.event_replay` over `MockBilling`, a billing platform with its own dead-letter queue. Queue and ledger are one system with one database, so the revision check sits in the write's own transaction |
| D2 | All or nothing | Done: one transaction checks every event and the day's limit, then applies the batch or nothing. The readers' A/B/C test is a unit test and a scripted case |
| D3 | Each event under its original delivery key | Done: delivered events are left out at preview and skipped at the write; read-back checks the batch by GAWE's key and each event by its own key |
| D4 | Facts from records; per-batch rules; a daily cap with atomic reservation | Done: both tools are prepared, so the tenant comes from the run; per-batch rules on the frozen count, total and customers; a `counter` source on what is left of the day, so startup requires the platform's reservation, the first shipped use of it |
| D5 | The model never sees payloads | Done: the list returns metadata only; a payload with injected text never reaches a step output or the card |

Settled during the build:
- Another tenant's event fails as `event_not_found`, so the error reveals nothing.
- A batch of only delivered events fails as `nothing_to_replay`.
- The approval card shows any typed list of flat records as a table.
- The app joined the unreadable-input suite and boundary registry in S2, not S3. That suite now
  skips "missing" for optional fields and draws non-lists as the wrong type of a list field.
- `bench replay` writes a run directory like `bench integrity`.

Owner decision during the build: `billing` is an IT control system, since the billing platform
holds what replay's policy and preparation trust. Three scripted lateral-grant records gained
`control-system-change-needs-security`, with the same outcome; the clean set didn't move.

### S5. Verification

Review of the cumulative diff (30 files over five commits):
- **Scope:** the mock platform, the app, the card table, the dev seed's `billing_operator` role,
  the IT setting, the bench command, tests and docs.
- **Compatibility:** no engine change. `AppBundle` and `Stores` are unchanged.
  `VersionedRecords` gains a public `transaction`. The card's typed rows gain an optional
  `table`. IT behaviour changes for billing write only, by the owner's decision.
- **Privacy:** payloads stay in the platform. The approval card shows event ids, customers and
  amounts, to finance approvers in the same tenant.

Negative matrix on exported copies of `HEAD`, after a control run that passed: 6 of 6 caught.

| Mutation | Caught by |
|---|---|
| The platform deduplicates on GAWE's key only (no delivery-key or status check) | the late-delivery tests (mock and app), the not-failed test, the replay bench cases |
| No revision check at the write | the mock's one-bad-item test and the conformance test |
| No reservation | the race and approved-before-the-day-filled tests, the per-day tests, conformance, the tripwire |
| Preparation skips the tenant check | the other-tenant test and the replay bench cases (the platform's own check still refuses the write) |
| The list returns payloads | the payload test and every listing test (the output schema refuses the extra field) |
| `billing` not a control system | the M23 control-systems check and the grant boundary oracle |

The revision-check mutation is caught only at the mock level. In the app-level A/B/C test, B's
amount changed, and the platform also refuses on the amount. The revision check is what catches
a change to anything else (its customer, say), as the mock tests show.

```text
make check: ruff clean; mypy clean; 3152 passed, 84 skipped
make test-integration (Postgres): 126 passed
make ui-check: 42 tests, build
make check-docs: checked 259 references, 0 problem(s)
python -m bench scripted: 38 records, identical to the post-billing-decision run
python -m bench integrity: 23/23 match
python -m bench replay: 9/9 match
make test-schema, alembic: not run; no schema change
```

Limits that stand:

- Mocks only. A real platform must check revisions in the write's own transaction, deduplicate
  on each event's original delivery key and reserve against a daily limit, or replay is
  prepare-only.
- The caps are proposals, not measured.
- All or nothing sends a whole batch back to preview when one event changes.
- The IAM's `billing` and the platform are linked by name only, kept by hand.
- The agent adds little over a careful replay script. What it shows is that a model in the loop
  can't widen, cross or repeat what a person approved.
