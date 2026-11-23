# M22 design: What the target guarantees

Status: **Done.** S1–S3 and the limits follow-up approved and committed by the owner and merged to
`main` by fast-forward (2026-11-23). D1–D5 approved as drafted on 2026-11-22. No paid runs. The plan and its progress
record are in [m22-plan.md](m22-plan.md); results are in the build record at the end.

## 1. Purpose and boundaries

Queue item 8 merges two reader requirements. Both ask what a target system actually enforces and
what GAWE only assumes. This design proposes doing the first now and the second as M23 (D1):

- **What the target guarantees (M22).** An approval can go stale while the configuration stays
  the same, because live state moves underneath it. For every condition policy relies on, name the
  authoritative system, how fresh the value must be at the moment of the write, and whether the
  target enforces it there or GAWE only checks it just before. Cumulative limits need atomic
  reservation at the target, not separate numeric checks. When the target can't enforce what a
  write needs, refuse automatic execution and name the race that remains; a fresh approval doesn't
  remove it.
- **Credentials and controls the agent can't touch (M23).** A read credential can't write; tools
  can't change the policies, verifiers or credentials that constrain them; the audit trail links
  proposal, decision, approval, dispatch and observed result for every outcome.

In scope for M22: a declared list of the guarantees each write relies on, a startup check, a
dispatch-time check for conditions the target can't enforce, the gap found below, and tests. Out
of scope: a real connector, cumulative limits as a feature (none exist yet; D4), and everything in
M23.

## 2. What the codebase does today

Conditions policy reads from a live system, and how fresh each is when the write happens:

| Condition | Authoritative system | Fresh at the write? |
|---|---|---|
| Deal value (signature, assessment) | Contract system | **Yes.** The contract's revision is bound into the action hash; the contract system checks it in the write's own transaction (M16b) |
| The user account (grants) | IAM system | **Yes.** Same, with the account's revision |
| Email recipients | Directory, mail server | **Yes, checked just before.** The expansion is bound into the hash; the executor refreshes it at dispatch and refuses a change (`RecipientChanged`); the mail server reads back what it accepted (M16a) |
| Ticket submitter and channel (grants) | Ticket system | Fixed when filed; can't change |
| **Ticket status (grants)** | Ticket system | **No.** Checked once, at preparation. An approval can wait up to 24 hours |
| Tenant settings | The app | Constants per deployment |

**A gap, found while exploring:** a ticket closed while its grant waits for a security approver
still gets the grant when the approver says yes. Probe: an on-behalf request pauses for approval,
the ticket is closed, the approval is given, and the run completes with the grant written. The
IAM write's guard checks only the user account's revision; nothing reads the ticket again.

Other relevant facts:

- **Declared guarantees.** Only one exists: `conditional_writes`, the tool versions whose connector
  checks a bound record revision in the write's transaction. Startup refuses a tool that binds a
  record (`record_binding`) without one. Idempotency by key and an authoritative read-back are
  assumed by every write since M16a but never declared; the mocks provide both.
- **Labels are not lookups.** M16a's read-back uses each system's own record of the operation,
  keyed by GAWE's key. A system that only offers searchable labels or delayed history can't prove
  an operation didn't happen; absence there is not failure.
- **Cumulative limits.** None. No rule reads a running total.

## 3. Decisions for the owner

### D1. Scope

| Option | Scope | Problem |
|---|---|---|
| **A. M22 target guarantees, M23 credentials and controls (recommended)** | This design; the credential, protected-control and audit-link requirements get their own design next | Two milestones instead of one |
| B. Both in M22 | One milestone | Twice the size, with unrelated parts (mock credentials, audit linkage) |

### D2. Conditions the target can't enforce at the write

The ticket lives in a different system from the grant, so no single transaction can check both.

| Option | How | Problem |
|---|---|---|
| **A. Check again just before dispatch, and name the window (recommended)** | Each such condition is declared with its check. The executor re-reads it immediately before calling the tool, after all other checks, and refuses with `condition_changed` if it no longer holds. The run records which conditions were only checked, not enforced | A ticket closed between that read and the write still gets the grant. The window is milliseconds, it is named in the threat model, and the grant still expires |
| B. Bind the ticket's revision into the hash | Any change to the ticket, even a comment, invalidates the approval | Still not checked at the write; more false invalidations than A for no stronger guarantee |
| C. Ask for a fresh approval when the ticket changes | The reader's point: an approval doesn't remove the race | Declined |

### D3. When a write relies on a guarantee its target doesn't declare

| Option | Behaviour | Problem |
|---|---|---|
| **A. Startup refuses, unless the app makes that class prepare-only (recommended)** | Each write declares the guarantees it needs (derived: idempotency by key and authoritative read-back for every write; a conditional write for a bound record; atomic reservation for a cumulative limit). A connector that lacks one stops startup, unless that tool version is prepare-only (M20), where a person does it by hand | None found; it generalizes today's record-binding check |
| B. Silently hand such writes to a person | Nothing breaks at deploy | A configuration change quietly turns automation off; surprising |
| C. Allow it, with a warning in the audit trail | Keeps working | Exactly what the reader asked us not to do |

### D4. Cumulative limits

| Option | Scope | Problem |
|---|---|---|
| **A. The rule only (recommended)** | Startup refuses a write that relies on a running total (a policy source of a new kind, `counter`) unless its connector declares atomic reservation. A test app shows the refusal | No working example of a cap |
| B. Build one cap | For example, grants per person per day, reserved in the mock IAM, with concurrent requests against it | A feature nobody asked for, to demonstrate a property |

### D5. Where guarantees are declared

| Option | How | Problem |
|---|---|---|
| **A. By the connector, per tool version (recommended)** | `AppBundle.guarantees: (tool, version) -> {conditional_write, idempotent_by_key, authoritative_readback, atomic_reservation, conditions checked at dispatch}`, replacing `conditional_writes`. The mocks declare what they do | Every app declares more; both shipped apps change |
| B. In the tool manifest | Next to `record_binding` | The manifest describes the action, not the system; one tool can run against systems with different guarantees |

## 4. What changes, if approved

- `AppBundle.guarantees` (replacing `conditional_writes`) and the startup check that each write's
  required guarantees are declared, or the class is prepare-only.
- Dispatch-time condition checks: the IT app declares "the ticket is still open" for grants; the
  executor re-reads it last before dispatch and refuses `condition_changed`; the run view and audit
  record which conditions were checked, not enforced.
- A `counter` policy source kind and the startup refusal without atomic reservation.
- Tests: the probe above as a test (closed while waiting: refused, no grant); closed between the
  check and the write (the named race: granted, recorded); reopened before the approval (granted);
  each startup refusal; a prepare-only class allowed without the guarantee; every shipped write
  declares what it needs.
- Docs: threat model (a new row for stale conditions), API docs, roadmap, completion records.

## 5. Expected changes to existing numbers

None expected. The scripted benchmark and integrity suite don't close tickets mid-run; they should
stay identical, and any change is a finding to report.

## 6. Limits

- A condition checked just before dispatch still has a window; only the target can close it.
- The mocks declare guarantees they implement. A real connector's declaration is a claim someone
  has to verify against the real system.
- No cumulative limit exists to exercise reservation (D4).

## 7. Approval

Approved by the owner on 2026-11-22: D1–D5 as drafted, the name M22, and no paid runs. The build
plan is in `docs/m22-plan.md`.

## Build record

| Decision | Proposed | Result |
|---|---|---|
| D1 | M22 target guarantees; credentials and controls as M23 | Done; M23 is next in the roadmap |
| D2 | Re-check conditions in other systems just before dispatch; name the window | Done: `DispatchCondition`, the IT app's `ticket_open`, checked on every attempt immediately before the tool call; `condition_changed` / `condition_unreadable`; `condition.checked` audited by name and result |
| D3 | Startup refuses a missing guarantee unless the class is prepare-only | Done, generalizing the record-binding check; the exemption covers only the prepare-only class itself |
| D4 | Cumulative limits: the rule only | Done: a `counter` source kind, traced through variables; no app uses one |
| D5 | Declared by the connector per tool version | Done: `AppBundle.guarantees` replaces `conditional_writes`; conditions sit in their own field, `dispatch_conditions` |

The gap found while exploring is closed: a ticket closed while its grant waits for approval now
gets no grant. Scripted and integrity are unchanged.

### S3. Verification

Review of the cumulative diff (19 files plus the S3 test and docs): the guarantee vocabulary and
startup check, the mocks' declarations, dispatch conditions and the executor check, the run view
field, tests and docs. `condition.checked` carries condition names and a result, never a value
read. Contract review declares no condition and behaves as before.

Negative matrix on copies of the working tree, after a control run that passed: 5 of 5 caught.

| Mutation | Caught by |
|---|---|
| The dispatch condition skipped (also what checking it only before the approval amounts to, since preparation already reads the ticket) | the closed-while-waiting, race, unreadable, retry and audit tests |
| Checked once before the attempts instead of before each call | `test_a_retry_checks_again`, `test_the_ticket_closing_before_a_retry_stops_it` |
| A missing guarantee allowed at startup | the removal, prepare-only, running-total and exemption tests |
| The prepare-only exemption widened to every class once one is prepare-only | `test_one_prepare_only_action_exempts_only_itself`, added in S3 because nothing caught it |
| A running total not traced through a variable | the running-total test, through a variable |

```text
make check: ruff clean; mypy clean; 2549 passed, 79 skipped
make test-integration (Postgres): 121 passed
make ui-check: 38 tests, build
make check-docs: checked 211 references, 0 problem(s)
python -m bench scripted: 38 records, identical to M20's last run
python -m bench integrity: 23/23 match
make test-schema, alembic: not run; no schema change
```

Limits that stand:

- The window between the last read of a condition and the write remains; only the target can
  close it. A ticket closed in it still gets its grant, which then expires.
- No cumulative limit exists, so atomic reservation is required but never exercised (D4 as
  approved), and the conformance test has no check for it.

Two limits found in S3 were closed before the merge, at the owner's request: the run page now
shows the conditions a step is only checked against, and a conformance test checks every
declared guarantee against each mock's behaviour (17 cases; a declaration with no check fails).
Against a copy with the IAM's revision check removed, and one where the operations store keeps
each call under a new id, it caught both.

