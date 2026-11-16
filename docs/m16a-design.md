# M16a design: Sending and read-back

Status: **done** (2026-11-15). D1–D5 approved; S1–S5 complete and committed; S6 cumulative
verification passed and was committed by the owner.
The owner committed the design and approved D1–D5. The separately approved build plan is in
[m16a-plan.md](m16a-plan.md). Work is on
`m16a-sending-readback`, created from clean `main`. S2 is complete and was committed by the owner
on 2026-11-12.

## 1. Phase 0: purpose and boundaries

A signed decision proves permission for a specific action; a tool saying it succeeded does not
prove that action occurred. This milestone connects authorization to actual recipients and to
independently read system state. The intended order is directory expansion, policy and approval,
send-time revalidation, execution, then read-back deciding the step's outcome.

The four deliverables, in order, are:

1. Expand mailbox targets through a mock directory before hashing and evaluate every mailbox.
2. Independently read back the four write tools' effects, including mail-server accepted recipients.
3. Persist unresolved verification with a deadline and resolve it through a scheduled sweep.
4. Measure injection through tool outputs, short fields and an interpreting mock.

A pre-send check can prevent a send; a post-send mismatch can only detect an effect that has
already happened. A blocked verification must never be described as an undone email or revoked
grant. Likewise, server acceptance is not evidence of ultimate delivery, reading, or downstream
forwarding. These limits belong in results and threat-model wording.

Real mail/directory integration, automatic replanning, general record-version approvals, grant
expiry, reusable approvals, approval-content redesign, unreadable-decision markers and next-day
review are outside this design. No paid model calls. Existing internal-mail and routine-grant
policy choices remain unless the owner selects a different recommendation below.

## 2. Phase 1: findings from the code

| Area | Current behavior | Consequence for this design |
|---|---|---|
| `core/models.py`, `core/registry.py` | A manifest has one input schema and version; the registry retains versions, while new plans use the latest | Prepared email needs a clear separation between model-proposed and connector-generated fields; old pinned versions cannot silently become new actions |
| `engine/orchestrator.py::_evaluate_policy` | Resolves top-level `$ref` values, evaluates policy, stores resolved arguments and the signed decision | Directory preparation belongs after reference resolution and before policy/hash creation |
| `core/canonical.py::action_hash` | Hashes tool, version, all resolved arguments, run id and step id | The expanded set and marker can be bound as resolved arguments; lookup timestamps must be separate |
| `core/policy/engine.py`, `rules.py` | Schema/RBAC gates precede rules; domain operators handle one string; strongest outcome wins; incompatible approver roles deny | A recipient array needs explicit bounded list operators, with malformed values failing closed; checking only the original label is insufficient |
| `core/policy/provenance.py` | Checks every registered version's declared writes against variable sources | Directory reads must be trusted adapter input; no agent tool may write directory membership; new write scopes must remain declared |
| App `tools.py` files | All four write postconditions compare requested values with returned values | An echoing or lying tool can pass without an independent system read |
| `connectors/mock_email.py` | Records requested `to`, subject and body in the common ledger, then echoes `to` | There is no independent mail-server acceptance record and no group expansion |
| `connectors/mock_clm.py`, `mock_iam.py`, `mock_esign.py` | Writes append ledger records; IDs derive from the idempotency key; no separate effect lookup API | Add authoritative mock-system reads keyed by the engine's operation key, not by an untrusted reply ID |
| `core/executor.py` | Claims a step, retries transient tool errors under one idempotency key, saves output, then verifies it | Write retries and read-back retries must be separate; read-back must never cause another send |
| Executor recovery | Saved output prevents repeating the tool; `verifying` resumes from saved output; authorization runs first | Verification recovery must work after approval expiry without giving permission for another write |
| `core/statemachine.py` | `pending` means not started; `verifying` can only succeed/fail; no delayed-read status | Add explicit waiting/blocked/unknown verification semantics, including run settlement |
| `engine/recovery.py`, `api/background.py` | Startup recovery submits active runs; background work is a thread pool, not a scheduler | Add a periodic maintenance runner with durable due work; startup recovery and sweep must share ownership rules |
| `core/records.py`, `db/step_store.py` | Step records have no verification deadline, evidence or due-work query; transitions are CAS | Persist verification context and lease/revision information with equivalent memory/Postgres behavior |
| SQL migrations | Status CHECK constraints and application-role grants are explicit; latest migration is 0004 | New states/storage require a new migration and SQL/schema/store-contract verification |
| `core/approvals.py`, SQL approvals | Approval records retain the action hash; more than one historical approval per step is possible, only one pending | A recipient refresh needs a new hash and approval; it cannot reuse a previous approval decision |
| `core/audit.py`, `telemetry.py` | Audit retains payload hashes, not payload bodies; telemetry excludes arguments/output | Lookup timestamps and mismatch details need durable protected records; an audit payload alone cannot retain them |
| `core/refs.py`, Planner | `$ref` values pass directly from saved upstream output; no model reads those outputs today | Test poisoned contacts as arguments, without claiming to measure model susceptibility |
| Reader schema | Contract summary is a string bounded to 300 characters | An encoded synthetic payload can fit; exact-byte canary checks alone are insufficient |
| API/UI | Statuses are strings, but terminal-state classification and badge mappings enumerate known states | Expose waiting/unknown accurately and update lifecycle handling; no approval-screen redesign |
| Scripted benchmark | 38 existing case/mode pairs; 7 execute, 8 deny, 23 wait for approval | Keep this baseline separately identifiable while adding execution-integrity scenarios |

Existing useful tests include executor saved-output/lease/verification recovery, orchestrator
reference resolution and approval expiry, memory/Postgres store contracts and concurrency tests,
M14 unreadable-input properties, M15 boundaries/provenance/sequences, and the scripted benchmark.
They are regression constraints, not evidence that the proposed features already exist.

## 3. Phase 2 decision register

The owner **approved D1–D5 and their detailed semantics** on 2026-11-12.
The recommendation wording below records the alternatives considered at Phase 2.

| Decision | Recommendation | Alternative / tradeoff |
|---|---|---|
| D1 Email schema/version and migration | `email.send@2.0.0`; retain one requested `to` string in the proposal, add a required trusted delivery envelope to its execution input; retire v1 for execution | A public array of targets is possible, but adds multi-target proposal semantics and changes every existing scripted plan unnecessarily |
| D2 Waiting and terminal states | Step `awaiting_readback`; resolution to existing `succeeded` (done), new `verification_blocked`, or new `verification_unknown`; run `waiting_verification`, with mismatch -> `failed`, timeout -> new terminal `unknown` | Reuse `failed` for unknown with a reason, but that obscures the distinction between disagreement and missing evidence |
| D3 Scheduling, deadline and heartbeat | Generic maintenance loop attached to API lifespan, every 30 seconds, 5-minute deadline; structured log heartbeat plus OTel counts/observations; optional one-shot maintenance command | An external-only scheduler works for Postgres but cannot share in-memory demo state; no request-triggered-only resolution |
| D4 Read-back and retries | All four write tools require an independent reader; immutable operation identity, no write retry caused by read failure; read-only retries until the fixed deadline | Reply-only postconditions are insufficient; automatic compensating writes are outside scope |
| D5 Encoded bodies and interpreting mock | Measure and document the body-content gap; do not add a heuristic decoding/content policy | A new body policy would require an explicit language/encoding contract and new false-positive decisions; generic base64 rejection is not a reliable safety boundary |

Approval covers D1–D5. The architecture and test criteria below define the approved design;
the separately proposed build plan supplies the implementation sequence.

## 4. Directory expansion and the v2 email contract (D1)

### 4.1 Trusted preparation

Introduce a version-specific trusted preparation adapter in the application bundle. It consumes
schema-checked, reference-resolved proposal arguments. Only deterministic connector code can
supply prepared fields; user/model values for them are rejected, never merged or trusted.

Proposed schema separation:

- `proposal_schema`: optional manifest field, defaulting to `input_schema` for unchanged tools.
  The Planner catalog and raw-plan validation use it. For email it is the existing bounded
  `{to: string, subject: string, body: string}` object, with no extra properties.
- `input_schema`: the complete policy/executor schema. Email v2 additionally requires
  `delivery: {mailboxes: string[], directory_marker: string, reapproval_required: boolean}`.
  Mailboxes are nonempty, unique and sorted deterministically; maximum 100 resolved mailboxes.
  The boolean is initially false and becomes sticky true after a detected directory change.
- Persist `recipient_lookup_at` separately as preparation/approval metadata. Persist a snapshot
  alongside each approval so subsequent refresh does not overwrite the lookup time shown for an
  earlier approval. The timestamp never enters resolved arguments or the action hash.

Both schemas are validated at registration. Startup policy reference checks continue against the
execution schema for every registered version. Preparation requirements and adapters must match
by tool/version; a missing adapter prevents startup. Direct execution requires a valid signed
prepared action and repeats the directory check; bypassing the Planner cannot bypass expansion.

The directory uses one consistent snapshot per expansion and supplies an opaque monotonic change
marker. Recommend a global mock revision initially: unrelated directory changes also require
reapproval, a conservative and easily tested choice. Marker comparison is exact even when the
mailbox set is unchanged. Lookup time changing alone never invalidates an approval.

Model the mock entries explicitly: mailbox, alias, group, external list, unreadable group. Include
nested groups, an alias leading to a group, and internal groups containing external members.
Detect cycles, missing internal entries, malformed addresses, empty groups, depth over 16 and
expansion over 100 mailboxes; fail before sending. Never truncate or fall back to a group label.
Seed known singleton internal targets used by existing benchmark/test controls; unknown internal
addresses fail resolution. External plain addresses and external lists remain single external
recipients, so their domains still receive normal policy treatment. A known alias is expanded
regardless of whether its label looks internal or external.

Mailbox identity uses the M15 parser for domains, preserves local-part case and plus tags, and
normalizes only the domain representation to strict IDNA ASCII. Directory matching must use that
same declared identity contract. Duplicate canonical targets collapse to one envelope recipient.
The mock server accepts only explicit terminal mailbox/external-list targets; it rejects group or
alias targets and performs no group expansion on behalf of the app. The human-facing To header
may retain the requested label; headers cannot supply or override envelope recipients.

### 4.2 Policy over the expanded set

Recommend narrowly defined list-domain predicates on `delivery.mailboxes`:

- `any_domain_in`: true if any parsed recipient is in blocked domains.
- `any_domain_not_in`: true if any recipient is external to internal domains.

These are approval/deny predicates, not newly permitted allow predicates. Invalid/missing/empty
recipient lists cause an evaluation error/deny, including when the ordinary input-schema gate is
bypassed in tests. Invalid domain operands also error. Do not broaden scalar operators implicitly
or use vacuous truth for empty arrays. Keep the baseline email allow rule; replace the two email
domain predicates with their list forms and retain their rule ids. Blocked wins over external
review, including mixed internal/external/blocked groups. All legitimate internal expanded sets
retain the current automatic-allow policy. Add a rule on trusted `delivery.reapproval_required`
requiring legal review. No parsing of subject/body is added under D5.

Preparation failures are separately classified as execution-preparation blocks, with a safe code
and no send. They do not masquerade as successful policy decisions. Existing invalid-address tests
must distinguish parser/policy tests from the earlier preparation failure at the app boundary.

### 4.3 Revalidation, changed membership and fresh approval

Immediately before a new send, re-expand the original requested target and compare both canonical
mailboxes and marker with the signed snapshot. Do this for automatic and human-approved sends.
The mock directory's snapshot check and server acceptance must share a guarded submission boundary
so a test can inject a mutation between checks without a race permitting a group send. Real atomic
semantics are not claimed for future connectors.

A mismatch performs **no send** and invalidates permission to use that snapshot. Recommend a
bounded deterministic recipient refresh: prepare a new snapshot of the same requested action,
set `reapproval_required=true`, rerun policy and sign a new hash. Deny remains deny; otherwise the
new snapshot waits for fresh legal approval, even if every recipient is still internal. Old
approvals never authorize the new hash. This is not model replanning: no new plan or model call,
no changed subject/body, and no automatic send. If membership is unreadable, stop with a failure
code instead of creating an approvable unknown recipient set.

The refresh requires an explicit pre-dispatch transition back through policy/approval and durable
reconciliation of snapshot, decision and approval. Allow it only when the system can establish
that no write has been dispatched for the old operation. Once dispatch may have occurred, resolve
that operation by idempotency/read-back first; never mint a second operation key to work around
uncertainty. Recovery must reconcile partial refresh persistence without sending from a stale
hash. A later membership change repeats the fresh-approval requirement, not a silent loop/send.

### 4.4 Existing versions, rules and benchmarks

Recommend registering only email v2 for new execution. Never reinterpret saved v1 resolved args
or signatures as v2. Old unexecuted v1 steps fail closed with an unsupported-version code and need
a newly submitted request; already terminal history remains readable. Drain pending v1 sends
before deployment. Legacy in-flight v1 effects lack the new envelope evidence: explicitly flag
for operator reconciliation, never resend or claim verified success. This compatibility cost is
part of D1, not a silent migration.

Existing scripted proposals retain `to`, subject and body; the real preparation path enriches
single-mailbox cases. Direct policy fixtures must intentionally build valid prepared envelopes,
with separate adversarial tests for forged/missing envelopes. Update scalar-to-list rule boundary
coverage, M14 operator properties and M15 sequence fixtures without relaxing expected outcomes.
The original 38 case/mode results should remain 7 executions / 8 denials / 23 approvals. Keep new
group, output-poisoning and interpreter scenarios labeled separately so the old denominator and
expectations cannot change unnoticed. Email effect matching uses server-accepted recipients,
not the request label or app-intent ledger. Other write tool schemas need no gratuitous version bump.

## 5. Independent read-back (D4)

Add app-owned read-back adapters, required at assembly for each of the four shipped write tools.
These are privileged infrastructure calls, absent from the Planner tool catalog. They are read-only
and must not write policy-variable source resources. Existing output schema validation remains a
transport check; a valid reply is never sufficient for a write step to succeed. Read tools retain
their current output validation.

Use the engine-derived idempotency key as the lookup identity. A returned message/record id may
be retained as diagnostic data but cannot redirect verification to another matching record.
Mock systems persist their own records independently of the app ledger, keyed by that operation,
and reload them after restart. The app's intent/effects ledger is not authoritative verification
input. Fault injection must independently control reply lies, accepted/applied state, delayed
visibility and read failures. Email has a distinct mock mail-server component with its own
accepted RCPT TO set; changing headers or the app ledger cannot change that evidence.

| Tool | Fresh authoritative evidence compared with the signed action |
|---|---|
| `crm.write_assessment` | Operation identity, contract id, risk level and stored summary |
| `iam.grant_access` | Operation identity, account, system and level |
| `email.send` | Operation identity and exact accepted recipient set; also subject/body identity via bounded digests where stored by the server |
| `esign.send_for_signature` | Operation identity, contract id, signer and deal value; retain the existing pre-write deal-value check |

These reads prove the operation record, not that no later authorized action ever changes current
state. A mismatch records missing/extra/different fields and blocks success. Returned output that
fails schema validation is still a protocol failure; if a write may have occurred, retain and
reconcile its independent evidence rather than pretending nothing happened. An adapter must
return explicit agreement, disagreement, or unavailable, not a truthy arbitrary response.

Persist expected/observed structured differences in a tenant-scoped verification evidence record,
not in log strings. Keep raw documents/message bodies out; store field names, bounded recipient
sets and values needed for the difference, plus digests for large text. Authorize access like the
run itself. Audit records the evidence digest and result code only; telemetry contains counts and
status labels. An evidence record is necessary because the audit log does not store payload bodies.

### Retry and crash contract

- Known pre-dispatch transient write failures may retain the existing bounded write retry policy,
  under the same operation key and with send-time recipient validation before actual submission.
- A returned write, transport timeout after possible acceptance, or recovery after uncertain
  dispatch goes to reconciliation. Read failure never creates a fresh write attempt or key.
- Connector idempotency must persist with its authoritative record. If a call is recovered by
  idempotency lookup, return/reconcile the existing effect; do not re-send under changed recipients.
- Persist dispatch/verification intent and a fixed deadline before the external-call boundary.
  A crash cannot reset the deadline or make an uncertain dispatch look like an unsent action.
- Verification recovery checks the bound action and operation identity, but does not require an
  approval still to be unexpired merely to read an effect already dispatched. This exemption must
  have no path back into a write. Expired approval still prevents any new dispatch.
- A mismatch or timeout never triggers automatic compensation or another write. Unknown means
  the effect may already exist and must not be retried as a new operation automatically.

## 6. Waiting states and durable resolution (D2)

Proposed transitions for a dispatched write:

| Current step state | Condition | Next step state | Run behavior |
|---|---|---|---|
| `executing` | Dispatch returned or its outcome needs reconciliation | `verifying` | No downstream output consumption yet |
| `verifying` | Fresh read agrees before deadline | `succeeded` | Continue existing plan |
| `verifying` | Fresh read disagrees | `verification_blocked` | Fail run; retain difference |
| `verifying` | No usable read before deadline | `awaiting_readback` | `waiting_verification` |
| `awaiting_readback` | Fresh read agrees before deadline | `succeeded` | Return run to running and continue existing plan |
| `awaiting_readback` | Fresh read disagrees before deadline | `verification_blocked` | Fail run; retain difference |
| `verifying` or `awaiting_readback` | Deadline reached without a usable read | `verification_unknown` | Terminal run `unknown`; never report completed |

Terminal verification outcomes have no automatic outgoing transition. At or after the deadline,
resolve an unresolved job as unknown; do not silently extend it or use a later read to rewrite
history. Persist due time, deadline, operation identity, observation status and evidence. The
existing `pending` remains not-started. Schema/authorization errors before dispatch still use
existing deny/failure semantics, not verification-unknown.

Dependencies can consume output only after success. A waiting run does not busy-loop or start
new work while waiting; `_step_forward` must report no progress on an unchanged waiting step.
Unknown/blocked prevent downstream steps. If multiple states coexist, in-flight reconciliation
must not be discarded by a sibling failure. Run status is settled from persisted step outcomes,
not from a tool reply or from a heartbeat.

Cancellation stops future actions but does not undo an effect or delete pending reconciliation.
The sweep still closes its evidence job; it must not resume a cancelled run. Preserve the cancelled
run label and expose its final verification result separately. Any post-terminal audit append
requires refreshing the external audit head when anchoring is configured. No automatic reopening
of terminal unknown/failed/cancelled runs is proposed.

## 7. Scheduled sweep and heartbeat (D3)

Recommend a small generic maintenance runner with registered job kinds and a `tick(now)` interface.
Read-back is its first job kind; do not build unrelated jobs. Start it through API lifespan with
clean shutdown; default interval 30 seconds. It runs regardless of incoming requests, even when
there are no active runs. Default verification deadline is five minutes from persisted initial
dispatch intent, configurable to positive bounded durations for deployment/tests. Clocks are
injected in tests; no real-time sleeps are necessary.

Persist maintenance work in Postgres for durable deployments, with an equivalent in-memory store
for demos. Jobs need stable identity including plan/step version and operation key, due time,
deadline, status, claim lease and revision. Claim work atomically, process bounded batches, and
retry expired leases. A stale worker cannot overwrite a newer result. Use due-time pagination so
one failing record cannot starve others. State/result persistence and job completion must be
transactional or recoverably reconciled; a crash between success and run continuation must not
lose the continuation. Sweep, explicit resume and startup recovery use the same verification
resolver; none of them redispatches a waiting write. Extend startup recovery to recognize waiting
runs and reconcile interrupted verification before ordinary plan continuation.

Proposed operator entry points: `python -m gawe.maintenance sweep --once` and `make sweep` for a
single pass against the configured durable stores; the normal API process provides the recurring
schedule. A standalone invocation must reject a separate ephemeral in-memory store. It must not
construct a model provider or perform planning. Continuing an already materialized plan uses the
normal executor gates; no automatic replan is introduced. Durable mocks must reopen the same
system-state directory as the API. Multiple runners are safe through leases/CAS, not through an
assumption that only one scheduler process exists.

Every completed tick emits a structured `gawe.maintenance.heartbeat` log including job kind,
start/end time, checked count, resolved-done count, resolved-blocked count, newly-unknown count,
waiting count and total unresolved unknown count. Emit a heartbeat counter and numeric backlog
observations through the existing telemetry abstraction as well. Failed ticks emit an error
heartbeat, never a misleading successful zero-work heartbeat. No recipient or payload text goes
in the heartbeat. Per-step outcomes remain audit events; there is no synthetic global audit run.

Define the alarm against backlog snapshots: unknown count increasing across two completed ticks
while successful heartbeats continue. A separate stale-heartbeat condition detects a stopped
scheduler. Demonstrate both with a deterministic test and document example operator queries;
external alert-routing infrastructure is not part of this milestone. Because unknown is terminal
for automatic verification, its unresolved count remains visible rather than disappearing when
removed from the due-work queue.

New persistence and status constraints require a new migration, memory/Postgres store contracts,
lease/concurrency tests, least-privilege role access and `make test-schema`. API status/evidence
views and UI terminal/waiting labels must match. Do not silently suppress waiting/unknown in
benchmark classification or report them as completed writes.

## 8. Tool-output and short-field injection (D5)

Use free scripted providers and deterministic connector fault modes. Separate the following
questions: did poisoned data reach an argument, did policy permit it, what did the target accept,
and what did the interpreting mock do? They are not measurements of real-model attack rates.

- Return a poisoned `counterparty_contact` from the contract lookup; use a valid top-level `$ref`
  as email `to`. Blocked external targets deny, other external targets require approval, internal
  aliases/groups expand and are judged by their members. Invalid/unreadable targets block before
  dispatch. Assert resolved values, rule ids where policy ran, and actual accepted recipients.
- Put a bounded base64 payload in the Reader's 300-character summary and script an internal email
  carrying it. Use an opt-in test mock with an explicit miniature grammar, such as
  `{{b64:<payload>}}`, decoding only a bounded synthetic marker into a local test effect. No shell,
  eval, filesystem command execution or network access. Ordinary mocks do not interpret bodies.
- Include plain/encoded equivalents, malformed encoding and harmless formatting controls. Track
  decoded semantic markers separately from literal canary persistence. Also exercise an interpreter
  fault that adds recipients: independent server read-back must mark it blocked even if the reply
  echoes the approved address; the already accepted extra recipients remain an observed effect.
- Under the recommendation, an encoded body reaching the approved internal recipient can pass
  recipient policy and produce the synthetic downstream marker. Record this as a known permitted
  content gap, not as success of a new body filter. A decision to prohibit it requires revising D5
  before implementation; do not quietly relabel that case as blocked.

## 9. Acceptance criteria and verification scope

The later plan must preserve these design-level acceptance criteria:

- Group/alias never reaches the mock server as an envelope target; unreadable/nested/cyclic groups
  never fall back to labels; external lists remain external singleton recipients.
- Added/removed members and marker-only changes invalidate prior permission; fresh approval is
  required; lookup-time-only changes leave the hash stable. Forged prepared fields cannot pass.
- The server records acceptance independently. Missing/extra recipients, wrong CRM risk/account/
  signer and a lying tool response cannot result in step success.
- Delayed reads resolve to done, blocked or unknown at a fixed deadline; a sweep works with no new
  request/run. Test restart, duplicate sweeps, lease expiry, cancellation, expired approvals and
  crash windows around dispatch and result persistence; no duplicate writes or lost evidence.
- Heartbeats expose both resolutions and unknown backlog; increasing unknowns and missing
  heartbeats are distinguishable. Bounded due-work processing does not starve later jobs.
- Existing M14/M15 properties remain effective with the new schema/list predicates. Original
  scripted expectations remain explicit; new attacks have their own expected labels and counts.
- `make check`, `make check-docs`, schema checks for persistence changes, applicable store tests
  and UI checks for status changes pass. Run the free scripted benchmark and compare each case,
  not merely the exit code. Record commit/dirty metadata and actual outputs.

## 10. Related limits recorded, not expanded into implementation

The sorted mailbox set in the action hash is the actual recipient binding; the directory marker
is an extra conservative check. Email v2 requires a marker, and the global mock revision forces
reapproval even for unrelated directory changes. For a real directory connector, introduce a new
email tool version that either makes the marker optional (binding the set alone when the directory
provides no marker) or uses a per-group marker. This is deferred connector/version work, not a
change to v2. Carry this limitation into the S6 completion record.

Real-directory completeness, forwarding after acceptance, external-list membership, trustworthy
real-system reads, delivery receipts and real distributed atomicity remain connector work. General
record-version approval binding, content safety, grant lifetimes and full multi-worker run leasing
remain separate concerns. This milestone does need concurrency control for its own maintenance and
verification transitions; it does not promise to solve all existing multi-worker orchestration.

Phase 2 is approved and its design was committed by the owner. Phase 3 was separately approved and committed by the owner. Implementation follows
[m16a-plan.md](m16a-plan.md), with a review stop after each checkpoint. No later milestone work
has started.


## Build record

S1 adds standalone mock directory and mail-server primitives; the shipped email route is not yet
switched to v2. Synthetic seeds include aliases, nested/internal/mixed/blocked-member groups,
an unreadable group and an external list. Domain identity follows M15; mailbox local parts retain
case and plus tags. Expansion is bounded to depth 16 and 100 unique terminal targets, and rejects
unknown internal targets, malformed addresses, cycles and empty/unreadable memberships.

Storage choice: the Python standard-library SQLite engine, in `mail-system.sqlite3` inside the
caller-supplied mock state directory. Directory revision/identity and server acceptance records
share a database so `BEGIN IMMEDIATE` guards expansion, revalidation and acceptance across
processes. The app's ledger is separate and never read as acceptance evidence. Full synchronous
commits persist the acceptance and operation key together. This is a mock-system implementation
choice, not an additional production database dependency or a change to the application's
Postgres schema. Existing SQL migrations are unchanged.

The server records canonical accepted recipients, a header label and subject/body digests.
Identical operation retries recover the committed acceptance even after directory changes;
changed content under the same key rejects. Lookup timestamps are excluded from request identity.
A raw acceptance boundary exists for connector tests and rejects nonterminal targets; the future
app adapter must use guarded `submit`, which also revalidates the expanded set and marker.
The primitives are not authorization substitutes, and no real email is sent.

Focused verification: 30 passed in 1.43s (`bench-results/m16a-s1-focused.txt`), including four
independent submitter processes, rollback after process termination before commit, reconstruction,
marker/member mutations at submission and attempted concurrent mutation after the check. The
initial two lint failures triggered the planned stop; the owner approved continuation, and the
formatting/import issues were corrected before these tests ran.

S1 full verification output (`bench-results/m16a-s1-check.txt`):

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 102 source files
pytest --cov=gawe --cov-report=term-missing
1711 passed, 39 skipped in 101.84s (0:01:41)
python scripts/check_threat_model.py
checked 92 references, 0 problem(s)
```

`git diff --check` passed. S1 was subsequently committed by the owner. No application Postgres schema
or UI changed; mock SQLite persistence and concurrency were exercised by the tests above.
At the S1 checkpoint, S2 activation, read-back integration, scheduled resolution and injection
measurements were pending.


### S2 implementation record

S1 was committed by the owner. S2 activates only `email.send@2.0.0`: proposal arguments remain
`to`, `subject`, and `body`; trusted preparation adds the sorted envelope, directory marker and
sticky reapproval flag before policy/hash creation. Lookup time stays in separate metadata and
in each approval's copied snapshot. The app sends through the guarded mock server; policy uses
list-domain checks and requires legal approval after any detected directory change.

Migration 0005 adds step preparation metadata and a signed policy decision, plus approval metadata
and argument snapshots. The step decision and hash are written atomically; run-level decisions
remain a compatibility mirror. Recovery uses the step's decision when a crash interrupts that
mirror update. Existing table-level application-role grants cover the added columns.

A proven pre-acceptance directory mismatch closes the attempt with `recipient_changed`, then
transitions `executing -> ready` with sticky reapproval metadata. Recovery recognizes that durable
attempt result without dispatching again. Preparation returns through policy and a fresh approval.
Transitions compare both status and the expected action hash, including an explicitly absent
initial hash, so stale initial preparation cannot replace a refreshed action. Accepted operations
recover under the original operation key, even when the directory changed during a crash.

The benchmark obtains email recipients from server acceptance records, including acceptance with
no application log. It uses app text only when its digest agrees with the server record. Existing
internal-email goals now match accepted internal recipients rather than the group header; their
original expected outcomes remain unchanged. Real systems and independent status-setting read-back
remain outside this checkpoint; the existing reply postconditions remain until S3.

Deployment: drain or cancel pending v1 sends before upgrading; apply migration 0005 and preserve
the shared mock-state directory. Unexecuted pinned v1 steps fail with `unsupported_email_version`.
In-flight v1 steps fail with `legacy_send_reconciliation_required` for operator investigation;
they are never resent or relabeled as verified. Terminal run history remains readable. No old
hashes or signatures are rewritten.

The owner approved continuation after the initial syntax/formatting and boundary-category failures.
S2 verification and checkpoint status are recorded in the plan; no later checkpoint had started
at that historical checkpoint.


Final S2 verification: 1758 passed, 42 database skips in the default suite; 70 Postgres
integration/store tests passed separately without skips; 75 SQL checks and 101 documentation
references passed. All 38 scripted outcomes retain the approved expectations. Full output locations
and the implementation checkpoint are recorded in [m16a-plan.md](m16a-plan.md). S2 was committed
by the owner on 2026-11-12; S3 had not begun at that checkpoint.


### S3 implementation record (2026-11-14; committed)

The owner approved continuation after the initial verification stop. S3 was initially implemented
and verified in an isolated checkout based on the S2 status correction, then applied to the original repository.
The owner committed S3 and its correctness-review fixes on 2026-11-14.

CRM/IAM/e-sign use independent operation stores alongside S1 mail acceptance. App assembly requires
read-back adapters for each write version. Operation keys include action hash and plan version;
lookup never trusts a returned record id or app-ledger echo. Comparisons cover the fields in
section 5, with bounded evidence and text digests. Reply-schema checking remains a protocol check.

Dispatch intent and the original five-minute deadline are persisted before an invocation. Only a
proven pre-dispatch rejection can retry, under the same key and live authorization. Possible
acceptance, lost responses and read failures cannot redispatch. Verification-only recovery checks
stored identity without gating a read on approval expiry. Legacy writes without original S3
intent are flagged rather than supplied an invented deadline or verified from their saved reply.

Migration 0006 installs waiting/blocked/unknown states and generic CAS maintenance storage.
Recoverable continuation persists result, then step state, then the audit evidence sequence,
then acknowledgment after the driver settles the run. A competing driver cannot acknowledge an
unapplied/unaudited result. Waiting starts no further work. Cancelled/failed runs retain pending
evidence and their terminal label; explicit resume can resolve evidence without resuming the plan.

API evidence is tenant-scoped and matched to the current action hash. Raw message bodies and
review summaries are excluded; the audit holds the evidence digest. Configured anchors advance
using append-only predecessor-linked checkpoints after verifying the protected prefix. Ordinary
write-once insertion still rejects a different anchor. Old timestamp tokens cannot certify a new
checkpoint. Conflicting independent file writers fail closed, retaining the existing local-file
trust limitation; a failed authority request leaves the previous checkpoint and later-event count.

Final checks: 1836 passed/59 database skips in the default suite; a separate 97-test integration/
store/Postgres run passed without skips; 84 SQL checks; Alembic upgrade to 0006; 111 documentation
references; TypeScript, 18 UI tests and production build. All 38 scripted identities, original
outcomes and accepted-effect flags match (7 executed, 8 denied, 23 approval pending). No live model,
mail/directory or timestamp-authority calls were made. See [m16a-plan.md](m16a-plan.md) for logs.

S4 scheduling/heartbeat and S5 injection measurements remain unstarted. S3 alone requires manual
resume or restart to revisit delayed visibility. Read-back proves the original operation record,
not downstream delivery, reversal or absence of later legitimate changes. Real atomicity and
connector guarantees remain outside this mock checkpoint. S3 and its review fixes are committed.


### S3 correctness-review fixes (2026-11-14)

S3 has now been applied to the original working repository; the isolated-checkout descriptions
above record the earlier verification environment. The owner requested fixes for two reproduced
review findings. Each new write invocation now renews its still-live job lease by CAS and checks
current run/step eligibility and action identity, including after the attempt-store call. A stale
or expired dispatcher can only reconcile; it cannot retry after another worker takes over or
cancels the run. Approval-expiry checks retain the known pre-dispatch failure outcome.

A directory rejection now commits its `recipient_changed` job result before closing the attempt.
Recovery can finish a still-open attempt from that durable result, then refresh recipients and
request approval. It never infers non-dispatch merely from a missing system record. Regression
tests cover crashes after either write, takeover during retry backoff or attempt persistence, and
independent Postgres workers. The owner committed these fixes with S3; no S4 work has begun.


### S4 implementation record (2026-11-15; committed)

Section 7 is implemented as approved. `gawe.maintenance.runner.MaintenanceRunner` is generic over
registered job kinds; read-back is the only kind. It claims due jobs in bounded pages ordered by
due time with an exclusive cursor, takes a short lease by revision CAS, and moves failing or
unacknowledged jobs back one interval. The read-back handler resolves only through the S3
resolver under that lease and continues the existing plan through the orchestrator; it holds no
executor, so a dispatched write cannot be sent again. Unplanned runs are never planned from here.

The API lifespan ticks at startup and every 30 seconds without requests and stops cleanly. The
one-shot `python -m gawe.maintenance sweep --once` (`make sweep`) uses durable stores only,
constructs no model provider, and reopens the same mock state. The verification deadline is now a
validated setting (default five minutes) bound into each intent. Heartbeats and telemetry carry
counts and timings only; failed ticks emit error heartbeats. The alarm separates unknown growth
between successful ticks from a stale, missing or failing heartbeat, and terminal unknowns remain
in the backlog. Store protocols gained a due-time cursor and outcome counts; no migration.

Verification: 1879 passed with 64 database skips; 103 Postgres integration/store tests without
skips; 124 documentation references; all 38 scripted identities and outcomes match the approved
baseline. Details, logs and limitations are in [m16a-plan.md](m16a-plan.md). S5 and S6 have not
started.

Follow-up at the owner's request: in the API, continuation after a resolved read-back runs on the
background pool rather than the sweep thread, so a slow next step cannot delay ticks or heartbeats.
Unacknowledged results now move back one interval even though the resolver cleared their lease.


### S5 implementation record (2026-11-15; committed)

Section 8 is implemented as approved under D5, as a separately labeled suite
(`python -m bench integrity`, 23 scenarios) that leaves the 38 scripted runs unchanged. A mock
contract-system fault hook poisons `counterparty_contact` in tool output only; a valid `$ref`
carries it into email. Blocked targets deny, external targets wait for legal review, aliases and
groups are judged by their members, and unreadable, unknown or malformed targets fail in
preparation before policy or dispatch. Records assert the resolved value, rule ids and the mail
server's accepted recipients.

An opt-in interpreting mail mock with a bounded `{{b64:...}}`/`{{upper:...}}` grammar records
synthetic markers in memory only. Payloads fit the Reader's 300-character summary. Records
separate literal canary persistence from decoded markers; plain/encoded, malformed and formatting
controls are included. The encoded body to an allowed internal recipient passes policy and
decodes: this is the documented content gap, not a block, and no body policy was added. An
interpreter fault adding a recipient is caught by accepted-recipient read-back even though the
reply echoes the approved address; the extra acceptance stays recorded. Threat-model rows T29
and T30 cite only the new tests.

Verification: 1929 passed with 64 database skips; 103 Postgres integration/store tests without
skips; 136 documentation references; 23/23 integrity scenarios match; all 38 scripted identities
and outcomes match the approved baseline. Details and logs are in [m16a-plan.md](m16a-plan.md).
S6 has not started.


### S6 completion record (2026-11-15)

S6 reviewed the cumulative change from `main` to the S5 commit and verified it on that
clean commit. No code change was needed. The review found only documentation gaps, now fixed: the
API reference lacked the verification endpoint, still said resolved arguments appear only in the
approval view (verification records carry bounded compared fields; text stays as digests), and
had no upgrade notes for email v2 and migrations 0005/0006; `.env.example` lacked the maintenance
settings. Threat-model rows T24 and T26 now carry the limitations below, and the results page
records the scripted and integrity runs. No approved security semantics changed. The scripted
suite's expectations did not change; the M16a test changes adapt fixtures to prepared v2 email and
the list-domain operators (an unparsable address now fails in preparation, before policy) without
relaxing an outcome. No generated files or results are tracked; the roadmap and other
repositories are untouched.

Required negative matrix, each with tests that fail when the guard is removed (checked by
mutating an exported copy, never the working tree): a group or alias reaching the server, a
membership or marker change after approval, unreadable membership, independent read-back
disagreement, and a waiting step passing its deadline. The S2–S4 concurrency, crash, cancellation
and idempotency tests ran with them on Postgres.

Results on the S5 commit (all logs under the ignored `bench-results/`):

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 119 source files
1929 passed, 64 skipped in 139.78s (0:02:19)   # the 64 skips are the database tests below
103 passed in 4.17s                            # make test-integration on Postgres, no skips
schema tests passed: 84 checks                 # make test-schema
0006 (head)                                    # Alembic upgrade on a throwaway database
checked 136 references, 0 problem(s)           # make check-docs, after these doc edits
UI: tsc passed; 18 tests passed, 0 skipped; production build passed
191 passed in 7.76s                            # negative matrix + S2–S4 scenarios, Postgres
mutation check: 14 matrix tests each fail with their guard removed; all pass unmutated
scripted: 38 identities unique; 0 differences from main or the approved S2 baseline
          7 executed, 8 denied, 23 approval pending; expectations in cases.yaml all met
integrity: 23/23 match their expected records; known D5 content gap shown by B02-encoded-marker
```

Completion against section 9 and the milestone's definition of done: all four items are built as
approved; every case that must block has a failing test; `make check`, `make check-docs`,
`make test-schema`, the Postgres store/integration tests and `make ui-check` pass; the scripted
benchmark matches every expected outcome with no paid model call. M16a is done.

What stays mock-only: the directory, the mail server and the CRM, IAM and e-sign targets are mocks
with their own SQLite records. Their atomic check-and-accept, deduplication and independent reads
are properties of the mocks; a real connector must supply them. Delivery, forwarding after
acceptance, outside-list membership and a real directory's completeness are not checked.

Read-back detects; it does not undo. `verification_blocked` means the effect already happened
(an extra recipient received the message, or a record holds other values); `unknown` means it may
have happened and was not repeated. Neither triggers compensation. Read-back proves the target's
record of the original operation, not delivery or the absence of later changes.

The D5 encoded-body case is a measured, documented gap, not a block: an encoded body sent to an
allowed internal recipient passes recipient policy, is accepted, verifies and decodes at an
interpreting recipient, while an exact-text canary check misses it. No body policy was added.

Deferred: the sorted mailbox set in the action hash is the actual recipient binding; the directory
marker adds a conservative check. V2 requires a marker, and the mock's global revision forces
reapproval after unrelated directory changes. A real directory connector should use a new email
tool version with an optional marker (binding the set alone when none is available) or a
per-group marker; v2 is unchanged. Recipients are re-expanded on every send, with no cached
expansion. A cache that re-expands only when a group's marker moves is safe only if that marker
moves on any change beneath the group, including nested groups and members' forwarding settings,
which needs a real directory.

Known limitations that still stand: the backlog query scans job records (no kind index); unknown
is terminal with no operator-clear workflow, so its backlog only grows; alert routing is not
built. While a read is still waiting on a run left `running` by a crash, a tick may read it more
than once (reads only), and a live dispatcher slower than its 30-second lease can be overtaken by
a runner's read, after which it can only reconcile. The integrity scenarios measure deterministic
controls once data is poisoned, not real model susceptibility, and cover Contract Review only; an
allowed internal recipient chosen by poisoned data is still sent without a person; the
interpreting mock is a miniature test grammar. Downgrading past migration 0006 needs a reviewed
data migration.
