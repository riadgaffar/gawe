# M16b design: Approval integrity

Status: **Done.** S1–S6 approved and committed by the owner (2026-11-16). D1–D6 and the build plan approved
by the owner (2026-11-15); approve-once (D5) is split into its own milestone and not built here.
Work was on its own branch, created from `main` after M16a was merged.
The build plan is in [m16b-plan.md](m16b-plan.md).

## 1. Phase 0: purpose and boundaries

M16a made sending mean what was authorized: recipients are expanded before the hash and the
target system's own record decides whether a write happened. M16b makes **an approval mean
exactly what the approver saw**, limits how long automatic decisions last, and adds review after
the fact. Six items, in this order:

1. Approvals bind the version of every record they refer to, checked as a conditional write.
2. Policy decisions carry a per-check "value was unreadable" marker (a new signed format).
3. No model-written prose next to the Approve button.
4. Automatic grants expire, enforced by the M16a sweep.
5. Approve once for a kind of action, under a daily cap.
6. A next-day review job, with a keyed-hash sample of clean "low" ratings.

What each one can and cannot claim:

- A record version proves the record the connector writes against is the one bound into the
  hash at policy time. It does not prove that record is true, or that it is the document the
  Reader read: the uploaded document and the contract system's record are not linked today.
  Where trusted values come from stays waiting for a real contract system.
- The unreadable marker explains a decision; it does not change any outcome. M14's rule that
  unreadable input never ends in allow stays the guarantee.
- The approval screen change removes model text from the place a decision is made. Whether
  wording sways approvers needs real approvers and stays waiting.
- Expiry bounds the risk accepted by allowing routine grants. It does not judge whether a grant
  was right; the requester rule (M15) and review (item 6) do that.
- Next-day review detects; like read-back it never undoes. The sampled review's miss rate needs
  volume and stays waiting.

No paid model calls. No real connectors. The scripted benchmark's 38 expectations
(7 executed, 8 denied, 23 waiting for approval) must hold, or any change is explained and
approved.

## 2. Phase 1: findings from the code

| Area | Current behavior | Consequence for this design |
|---|---|---|
| `core/canonical.py::action_hash` | Hashes tool, version, resolved args, run and step | A record version inside the args is inside the hash; nothing else is needed there |
| `core/preparation.py`, `api/wiring.py`, orchestrator | M16a trusted preparation: version-specific adapters add fields the model may not supply, before policy and hashing; a missing adapter stops startup | The natural place to fetch a record version. The model never supplies it, and the proposal schema rejects it |
| `connectors/mock_clm.py` | Contracts are an in-memory dict loaded from seed; no version, no way to change a record; CRM operations live in `crm-system.sqlite3` | Needs durable records with a revision, a mutation hook for tests, and the version check in the same SQLite transaction as acceptance |
| `apps/contract_review/tools.py::send_for_signature` | Reads the contract (`deal_value_mismatch` check), then writes to a separate e-sign store | Exactly the separate read-then-write item 1 forbids. The e-sign acceptance lives in a different SQLite file from the contract record |
| `connectors/mock_iam.py` | Users from seed; `grant_access` has no expiry, no revoke, no notion of active grants | Item 4 needs an end date on the grant record, a revoke operation and an active-at-time read |
| `core/policy/signing.py` | One HMAC body under `gawe.decision.v1`: step, hash, outcome, rule ids, reason, role | A new field cannot be added to the v1 body without breaking every stored decision. A versioned body is needed |
| `core/models.py::PolicyDecision` | Frozen, `extra="forbid"`; stored as JSON in `steps.policy_decision`, `runs.decisions` and approval-adjacent records | Old JSON lacks new fields; defaults must make it parse as v1. No SQL migration is needed for the JSON itself |
| `core/policy/rules.py::rule_matches` | Stops at the first false predicate; returns only a boolean | Recording per-check readability needs an evaluation that reports each evaluated check without changing which rules match |
| `core/audit.py` | Events store a payload **hash** only; payloads are not stored anywhere; event hash format `gawe.audit.v1` | Adding fields to a payload changes no event format, so the old chain verifies untouched. A review "from the audit trail" must rebuild payloads from engine-written records and match their hashes |
| `engine/orchestrator.py` | `policy.decided` payload is built entirely from the signed decision | The review can rebuild it exactly from the stored decision: audit hash → decision → action hash → resolved args |
| `migrations/sql/0001_init.sql` | A `policy_decisions` table exists but no application code writes it | Not used as a source; listed as a related problem |
| `core/maintenance.py`, `0006_readback.sql` | Jobs require run, step, action hash and operation key; `maintenance_jobs` has a foreign key to `runs` | A grant-expiry job fits as is. A daily review job has no run and needs a schema change or a separate store |
| `maintenance/runner.py` | Generic runner, one handler per kind, heartbeat per kind, bounded pages, leases by CAS | Reused for grant expiry and daily review; no second scheduler |
| `api/schemas.py::ApprovalView`, `ui/app/approvals/ApprovalCard.tsx` | Shows the YAML reason (trusted) and every resolved argument as raw JSON, including email body and assessment summary, right above Approve | Item 3's target. The API should send typed and free-text fields separately so the UI does not classify |
| `ui/tests/` | `node --test` with type stripping over `.ts`; no JSX rendering, no DOM library | A labelling test runs against a pure view-model module; rendering TSX would need a new dependency |
| Policies | Approval needed for: external email, changed recipients, contradicted or large-deal "low" ratings, every signature, admin and prod-db grants | Relevant to item 5: no message kind that needs approval today is internal and safe to make unattended |
| Benchmark | Scripted plans name tools without versions and get the latest | New tool versions with trusted fields leave the scripted plans unchanged if outcomes do not move |

Useful existing tests: M16a preparation and refresh, read-back and maintenance runner tests,
approval and signing tests, M14 unreadable-input properties, M15 boundaries and sequences, and
the scripted and integrity suites. They are regression constraints, not evidence that M16b exists.

## 3. Phase 2 decision register (owner decisions)

The owner **approved D1–D6 as recommended** on 2026-11-15, including the detailed semantics
in §4–§9. For D5 that means the split: §8.4 is not built. The wording below records the
alternatives considered.

| Decision | Recommendation | Main alternative / trade-off |
|---|---|---|
| D1 Record versions | Bind a version for `crm.write_assessment` and `esign.send_for_signature` (the contract) and `iam.grant_access` (the user account); email keeps its directory marker. Fetched by trusted preparation, monotonic revision, checked inside the write transaction. No version → fail in preparation. Changed version → step fails `record_changed`, no automatic refresh | A content hash instead of a revision; or reusing email's automatic refresh path |
| D2 Decision v2 and audit payload | `gawe.decision.v2` HMAC body adds `format`, an ordered `checks` list and a `policy_digest`; v1 decisions verify with the v1 body only; never fall back. `policy.decided` payload adds the same fields; the audit event format is unchanged | Checks for matched rules only (M14's sketch); or no policy digest |
| D3 Free text on the approval screen | Every argument leaf is declared `typed` or `free_text` in the manifest; startup fails on an undeclared leaf. The API sends typed fields, the hash, the diff and collapsed free-text headers with character length only | Inferring free text from the schema (string without enum/pattern) |
| D4 Grant lifetime | Every new grant carries `expires_at` = preparation time + 7 days (tenant setting, capped at 30), inside the hash. "Tied to the request" = the grant record names its run; renewal is a new run. IAM treats expired grants as inactive and the sweep revokes and reads back | Expiry for automatic grants only; or lifetime until a ticket closes (needs a ticket system) |
| D5 Approve-once | Split into its own milestone after M17. Public and external sends can never be a kind. Design answers recorded in §8 so the later milestone starts from them | Build a narrow version now (§8.4) |
| D6 Next-day review | Daily job via the M16a runner for the previous UTC day. Sample: HMAC-SHA256 under a dedicated review key over the action hash, 5% rate. Results in a tenant-scoped `review_items` table, a per-run audit event, heartbeat counts, API list and verdict, and a small UI page | Stable key (contract, clause, model version); API only; a higher rate |

The sections below give the detail each decision commits to.

## 4. Record versions bound into the approval (D1)

### 4.1 Which tools, and where the version comes from

| Tool | Referenced record | Bound field |
|---|---|---|
| `crm.write_assessment` | The contract the rating is about | `record.contract_version` |
| `esign.send_for_signature` | The contract being sent | `record.contract_version` |
| `iam.grant_access` | The user account receiving access | `record.user_version` |
| `email.send` | None beyond the directory | Unchanged: v2's `directory_marker` already binds membership |
| Read tools | Not acted on | Unchanged |

Each write gets a new tool version (`crm.write_assessment@2.0.0`, `esign.send_for_signature@2.0.0`,
`iam.grant_access@2.0.0`) using M16a's split: the proposal schema the model sees is unchanged;
the execution schema adds a required trusted `record` object (and, for the grant, `expires_at`,
see §7). A version-specific preparer reads the record version from the system of record and
adds it before policy and hashing. A model or `$ref` supplying `record` is rejected as with the
email envelope. Startup fails if a tool that declares a record binding has no preparer, or its
connector does not declare conditional-write support (fail closed: a connector without it
cannot run the tool, rather than fall back to read-then-write).

The version is an opaque revision string that changes on **every** mutation of the record, not a
content hash. A content hash returns to its old value when a record is changed away and back; a
revision does not, so an approval can never be satisfied by a record that was edited in between.

Considered alternative: one snapshot revision for the whole store instead of one per record.
It would give "which version of the facts the action used" as one value, but the records live
in different systems (contracts, user accounts), and refusing whenever a store-wide head moves
refuses for unrelated writes, the same cost as the directory's global marker in M16a. The
refinement that refuses only when a record the action actually read has changed since the
snapshot gives the same result as a per-record version while each write references exactly one
record; it is worth revisiting when an action reads several. The bound version already records
the facts used: it sits in the signed arguments, which the review in §9 reads. That includes
the bound contract's deal value, which the large-deal rule reads from the contract system.

### 4.2 The check happens where the write happens

The mock contract system becomes a durable SQLite store holding contract records with their
revision, the CRM operation records and the e-sign envelope records in **one database**, so
"current revision equals the bound revision" and "accept this operation" commit in one
`BEGIN IMMEDIATE` transaction, like HTTP `If-Match`. The existing `deal_value_mismatch` check
moves inside that transaction too, and the separate read before the e-sign write disappears.
For the IAM mock, the user records and grant operations share one database in the same way.

A test hook mutates the record at the exact point between a separate pre-check read and the
write; the write must reject. A second test changes the record between approval and execution.
Both end with no accepted operation and step failure `record_changed`, a proven pre-dispatch
rejection under M16a's rules (no retry, no read-back wait).

Real systems must offer an equivalent precondition (an ETag/`If-Match` on the CRM write; an
envelope created from a specific document version). That is connector work, recorded as a limit.

### 4.3 No version, changed version, old steps

- **Record without a version** (missing revision, unreadable record, unknown contract or user):
  preparation fails with a safe code (`record_version_unavailable`, `contract_not_found`,
  `user_not_found`) before policy or dispatch.
- **Changed version**: the step fails `record_changed`. No automatic refresh: unlike a directory
  change, a changed contract means the Reader's findings and the plan were about older content,
  so a new run is the honest next step.
- **v1 steps**: like email v1 in M16a, unexecuted pinned v1 steps fail closed with
  `unsupported_tool_version`; v1 read-back adapters stay registered so already dispatched v1
  writes still verify; nothing is resent or relabelled. Terminal history stays readable.

## 5. Unreadable marker and decision format v2 (D2)

### 5.1 What is recorded

`PolicyDecision` gains `format` (1 or 2) and `checks`, a tuple of records
`{rule_id, path, op, unreadable}`, plus `policy_digest` (SHA-256 of the loaded rule set's
canonical form). No argument value or operand is ever recorded.

Record every **evaluated** check of every rule whose tool, side-effect and risk selectors matched,
in rule order, stopping where evaluation stops (a short-circuited check is absent, never shown as
readable). Recording non-matching rules too matters: an unreadable address that makes an allow
rule's `domain_in` false explains a `default-deny` that matched-rules-only would leave silent.
Schema and RBAC early rejections carry no checks. Matching itself is unchanged; a property test
proves the outcome and `rule_ids` are identical with and without check recording.

Readability per operator, precise and tested:

| Operator | Unreadable when |
|---|---|
| `eq`, `ne`, `in`, `not_in` | value missing, or a list/object (operands are scalars; `null` is a readable scalar) |
| `gt`, `gte`, `lt`, `lte` | value missing or not a number (booleans are not numbers) |
| `exists` | never (absence is what it measures) |
| `domain_in`, `domain_not_in` | `email_domain(value)` is `None` |
| `any_domain_in`, `any_domain_not_in` | the list is invalid; evaluation already errors to `engine.error` deny, recorded as unreadable |

### 5.2 Signing and compatibility

v2 decisions are signed under a new domain, `gawe.decision.v2\n`, over a body that includes
`format`, `checks` and `policy_digest`. Verification dispatches on `format`: a v1 decision
(no checks, no digest) verifies with the unchanged v1 body; a v2 decision only with v2. There is
no fallback from a failed v2 check to v1, and a v1 decision carrying checks is rejected, so
stripping or forging the new fields breaks the signature. Old JSON without the new fields parses
as v1. New decisions are always v2.

Tests that must fail if compatibility breaks: golden v1 decisions (fixed key, fixed JSON) still
verify and still authorize execution after restart from memory and Postgres stores; an approval
pending on a v1 decision can still be decided and executed; tampering with any check, the digest
or the format fails; a v2 decision relabelled v1 fails.

### 5.3 Audit record

The `policy.decided` payload adds `format`, `checks` and `policy_digest`. Because events store
only the payload hash, the event hash format (`gawe.audit.v1`) is unchanged and every existing
chain verifies as before; a test verifies a recorded pre-M16b chain fixture. The payload is a
pure function of the stored decision, which is what lets item 6 authenticate decisions against
the audit trail (§9). The API exposes checks as rule id, path, operator and the boolean only.
No SQL migration is needed for this item: decisions are JSON columns.

## 6. Approval screen without model prose (D3)

### 6.1 How free text is identified

Each manifest declares, for every leaf path of its execution schema, `typed` or `free_text`
(for example `email.send`: `to`, `delivery.*` typed; `subject`, `body` free text;
`crm.write_assessment`: `contract_id`, `risk_level`, `record.*` typed; `summary` free text).
Registration fails if any leaf is undeclared or a declared path does not exist, in every
registered version. Inference from the schema is rejected as the primary rule because `to` is an
unconstrained string that must be shown, while a constrained-looking string can still carry
persuasion; an explicit, complete declaration fails closed when a tool changes.

### 6.2 What the approver sees

The API view gains, computed server-side from the snapshot the approval binds:

1. Typed fields first: recipient(s) (the expanded mailboxes for email), amounts, ids, levels,
   record versions, grant end date.
2. The action hash.
3. The difference from the last approved call: the most recent **approved** approval in the same
   tenant for the same tool name, typed fields only (added, removed, changed); free-text fields
   only as "changed" or "unchanged" by digest. "No earlier approved call" when there is none.
4. Free-text fields collapsed, each header reading "Written by the model from an untrusted
   document · N characters" (Unicode code points). No first line, no preview. Content appears
   only after an explicit expand, below the decision buttons' context.

The policy's reason (from YAML) stays. Raw `resolved_args` stay in the API for clients that need
them but the card no longer prints them as one JSON block. A UI test checks the labels, the
length, that collapsed headers contain no field text, and the field order, through a pure
view-model module (`ui/lib/approval-view.ts`). An API test checks the server classification.
Whether wording sways approvers is recorded as waiting.

## 7. Automatic grants expire (D4)

- `iam.grant_access@2.0.0` gets a trusted `expires_at`, prepared as preparation time plus the
  tenant's grant lifetime (default 7 days, validated to 1 hour–30 days). It is inside the hash,
  shown to an approver, and readable by policy. A rule denies a lifetime over the tenant maximum
  as defense in depth.
- Every new grant expires, approved or automatic. One lifetime keeps preparation independent of
  the policy outcome (preparation runs before policy). A longer lifetime for approved grants is a
  possible later setting.
- "Tied to the request": in the mock there is no ticket system, so the grant record carries the
  run id (the request) and its end date. A trusted ticket system, a later candidate, can then
  close grants early when its ticket closes. Renewal is a new request: a new run, new policy
  evaluation, new hash; there is no extend operation.
- Enforcement, three layers: the IAM mock reports a grant inactive at or after `expires_at`
  (the system's own rule, independent of the sweep); a `grant_expiry` maintenance job, created
  with the dispatch intent **before** the write (so a crash cannot leave a grant without its
  job), revokes at `expires_at` with its own operation key and confirms by read-back; the
  heartbeat reports overdue-but-active grants, and the alarm fires when that count is above zero.
  A revoke of a grant that was never accepted reads back as absent and completes.
- Revocation narrows access only and is trusted maintenance, not a policy-authorized action.
  It is audited on the grant's run (post-terminal append with anchor refresh, as in M16a).
- Grants made before M16b have no end date; they are listed for operator review, not backfilled.

Failing test required: a grant past its end date that is still active (mock reports active, or
the job is missing, or revocation is not read back) fails.

## 8. Approve once for a kind of action (D5)

### 8.1 Recommendation: split into its own milestone

It is larger than the other items together and, in the shipped workflows, has nothing safe to
apply to:

- The only messages that need approval today are external email and email after a directory
  change. Internal email to an unchanged directory is already allowed without a person.
- Making external email unattended reopens exactly the path the scripted attacks aim at
  (sending outside). A daily cap limits volume, not where data goes.
- Building it needs a standing-approval store, an owner screen to grant and revoke, a policy path
  that satisfies `needs_approval` from a standing grant under an atomic cap reservation, revert
  triggers from read-back, review and edits, and their tests.
- Its value depends on approval volume, which M17 measures on about 300 honest contracts.

### 8.2 Can public or external sends be a kind?

No, never. A yes to a public post or an outside message covers that one text; the risk sits in
the words and the audience, both of which change with every send. A "kind" can only cover sends
whose entire audience is internal and fixed, and whose content risk the organization already
accepts.

### 8.3 Answers recorded for the later milestone

- **Kind**: tenant, tool name and version, approver role, exact rule ids, and the exact typed
  arguments, including the sorted mailbox set and directory marker. Free-text fields may vary
  but appear in the next-day review.
- **Cap**: enforced by an atomic reservation in an engine-only table (conditional increment, no
  read-then-write), and reconciled every day by the review job against audit events, which no tool
  can write. Never counted from records the agent's tools write.
- **Reset**: UTC midnight. **Back to manual** on any rejected, blocked or unknown read-back, any
  review flag, any owner edit of the kind, or any change of the policy digest.

### 8.4 If the owner wants it built in M16b

The narrow version would be limited to internal-only email kinds as defined above, cap default 5
per day, owner role `legal_reviewer`, with the same revert triggers, and would add one
migration (standing approvals and reservations). Expect it to add roughly half again to M16b.

## 9. Next-day review (D6)

### 9.1 What runs

A `daily_review` job kind on the M16a runner, due at 01:00 UTC for the previous UTC day, one job
per tenant and day with a deterministic id. Because jobs today require a run, a migration lets a
job carry no run (a `scope` of `tenant` with run fields null, constrained by kind). Each review:

1. Lists the day's `policy.decided` audit events (an index on type and time is added).
2. For each, rebuilds the payload from the step's stored decision and checks it against the
   event's payload hash, verifies the decision signature, and recomputes the action hash from
   the stored resolved arguments. Any mismatch is a finding (tampered or superseded record).
3. For automatic actions that executed (allow, then `succeeded` or a verification outcome),
   re-evaluates the current written rules with the run's stored findings and the current
   tenant and system values. A different outcome is a finding, labelled "rules changed" when the
   decision's `policy_digest` differs and "inputs changed" otherwise (for example a deal value
   moved). v1 decisions without a digest get "rules may have changed".
4. Selects the sample (below) from automatic `crm.write_assessment` "low" ratings with no flagged
   clause.

### 9.2 The sample

`HMAC-SHA256(review_key, action_hash)` read as a number, selected when below rate × 2^256.
The key is a dedicated secret (`GAWE_REVIEW_SAMPLE_KEY`, at least 32 bytes, separate from the
policy signing key); each sample records the key id, never the key, so anyone holding the key can
reproduce the selection.

- Why keyed: the action hash covers the summary, which a document can influence. Without a
  secret, an attacker could vary wording until a downgrade falls outside the sample.
- Why the action hash and not a stable key: each run gets an independent draw, so the same
  contract is not permanently in or out. The trade-off: a contract reviewed repeatedly gets
  sampled sometimes rather than consistently. The stable key (contract, clause, model version)
  is the alternative if consistency per contract matters more.
- Rate 5%: zero misses in about 32 samples bounds the miss rate below about 9%; under 1% needs
  about 300. At current volume the miss rate stays waiting.

### 9.3 Where results go

A tenant-scoped `review_items` table (day, run, step, action hash, reason: `audit_mismatch`,
`signature_invalid`, `rules_changed`, `inputs_changed`, `sampled_low`; status `open`,
`confirmed`, `missed_risk`), a `review.flagged` or `review.sampled` audit event on the run, and
heartbeat counts per kind. An API lists items and records a verdict from a `legal_reviewer` who
is not the run's requester; a small UI page lists open items with confirm and "missed risk".
Verdicts are what will later measure the miss rate.

## 10. Waiting, limits and related problems

Waiting, not built: where trusted values come from (needs a real contract system); whether
approval wording sways approvers (needs real approvers); the sampled review's miss rate (needs
volume). Approve-once is split into its own milestone (D5), not waiting on M16b.

Limits carried into the threat model (T31–T35):

- **Mock-only checks.** Conditional writes, revocation and the active-grant rule are properties of
  the mocks. A real connector must supply `If-Match`-style preconditions, revocation and
  time-bound access.
- **What a version binds.** A record version binds the system's record at policy time, not the
  uploaded document.
- **Detect, not undo.** Review and revocation detect and narrow; neither undoes an effect already
  made.
- **The rule-set digest** identifies the rules, not the tenant variables they were evaluated with.
- **"Typed" decides placement, not trust.** A typed field can still hold a model-chosen value, such
  as a recipient address.
- **Review reach.** The review covers the last seven days and re-evaluates with today's values,
  so `inputs_changed` cannot say which value moved. It does not re-evaluate retired tool
  versions; their decisions still get the tampering, signature and action-hash checks.
- **The sample key.** Anyone holding it can predict the draw; without it the sample is off.
- **Duplicate audit events (fixed after M16).** Two sweepers whose leases overlapped could each
  write the same read-back, grant-expiry or review event. Since the follow-up fix, these events are
  recorded once: the write checks the chain for the same event inside the append's retry.
- **No paging.** The overdue-grant alarm is a health signal; nothing pages anyone.

Related problems found, not fixed here:

- `policy_decisions` (migration 0001) is never written by the application; decisions live in JSON
  on steps and runs. It could mislead a reader of the schema.
- Audit payloads are not stored anywhere, so only payloads rebuildable from engine records can be
  checked later. Decisions are; most other events' payloads are not.
- A step's decision is overwritten on an email recipient refresh, so the earlier decision's audit
  event can no longer be rebuilt. The review reports it as superseded rather than tampered.
- Grants made before M16b never expire. A backfill would need a decision on a fair end date.
- UI tests cannot render components; a rendering test would need a new dependency.
- The migration 0006 check on a job's stored run id let a null comparison through. Migration 0007
  replaces it, and a schema test now covers the case.

## 11. Approval

D1–D6 approved by the owner on 2026-11-15, as recommended. Approve-once is split into its own
milestone after M17, starting from the answers in §8.2–§8.3. The build plan is in
[m16b-plan.md](m16b-plan.md); the owner approved it on 2026-11-15.

## Build record

### S1. Record versions checked at the write

The contract mock now keeps its records in `clm-records.sqlite3` in the mock state directory
(in memory without one), seeded once from `clm_seed.json` when first read. Each record has an
opaque revision (a random value) that changes on every mutation. CRM assessment and e-sign
envelope operations live in the same database, so the revision check, the e-sign deal-value
check and acceptance commit in one `BEGIN IMMEDIATE` transaction. The IAM mock does the same in
`iam-records.sqlite3` for user accounts and grants; each grant also stores its `expires_at`,
lifetime and run id. Nothing is written to disk until a record is first read.

`crm.write_assessment@2.0.0`, `esign.send_for_signature@2.0.0` and `iam.grant_access@2.0.0`
keep the 1.0.0 proposal schemas. Their execution schemas add a required trusted `record`
object; the grant also adds `expires_at` and `lifetime_hours` (preparation time plus the
tenant's lifetime, default 168 hours, validated 1–720 at startup). The planner never sees these
fields, so a plan that supplies them is refused at planning; a `$ref` that resolves to an object
fails the proposal check before policy. An unknown contract or account fails in preparation
(`contract_not_found`, `user_not_found`), an unreadable or empty revision with
`record_version_unavailable`. A changed revision fails the step with `record_changed` as a proven
pre-dispatch rejection: no retry, no read-back wait, no refresh. The e-sign tool no longer reads
the contract before the write.

Settled within the plan:

- Manifests gain `record_binding` and `retired`. Startup fails if a bound tool lacks trusted
  preparation or a connector declaration of conditional writes (`AppBundle.conditional_writes`,
  filled from each mock's `CONDITIONAL_WRITES`).
- The 1.0.0 writes stay registered as retired, with read-back adapters over the older
  `crm-system`, `esign-system` and `iam-system` stores. An undispatched step pinned to a retired
  version fails `unsupported_tool_version` (the executor refuses with the same code). Retired
  versions are excluded from rule-path validation, since they never reach policy.
- Policy reads the lifetime as a number: rule `grant-lifetime-over-maximum-denied` compares
  `lifetime_hours` with `$tenant.max_grant_lifetime_hours` (720), declared from tenant
  configuration. The execution schema bounds it to 1–720 as well, so the rule only fires for a
  tenant maximum below 30 days.
- The benchmark counts effects from both the new and the older stores.

Visible change: a grant to an account that is not in the IAM system now fails in preparation
(`user_not_found`) instead of reaching the external-account deny rule. Both stop the grant; the
rule still denies a known external account. No scripted or integrity case uses an unknown account.

Limits: the revision check is mock-only; real systems need an equivalent precondition
(If-Match on the CRM write, an envelope created from one document version). Grants from before
M16b have no end date. Expiry is stored but not yet enforced (S4).

S1 verification (`bench-results/m16b-s1-*.txt`):

```text
make check: ruff clean; mypy: no issues in 123 source files;
            2009 passed, 64 skipped in 169.75s
make test-integration (Postgres): 103 passed, 0 skipped
make check-docs: checked 136 references, 0 problem(s)
python -m bench scripted: 38 records, 0 field differences from the M16a S6 run;
            8 denied, 23 waiting for approval, 7 executed
python -m bench integrity: 23/23 match; every case line identical to M16a S6
```

New tests are in `tests/unit/test_record_versions.py` (44): a record changed between approval and
execution, changed between a separate read and the write, and changed away and back, for all
three writes; forged bindings refused at planning and through a reference; unknown and
unreadable records; startup checks; two cross-process tests on the shared database (a committed
change is seen by the guard, and a change cannot land between another process's check and its
write); undispatched 1.0.0 steps failing closed and dispatched 1.0.0 writes still verifying.
The read-back mismatch test also covers the bound revision, end date and lifetime.

### S2. Unreadable marker and signed decision v2

`PolicyDecision` gains `format`, `checks` and `policy_digest`. JSON without `format` parses as
format 1; a format 1 decision with checks or a digest fails validation, and a format 2 decision
without a digest does too. `RuleEngine` computes the digest once at load (SHA-256 of the
canonical rule list) and issues only format 2. The signer seals format 2 under
`gawe.decision.v2\n`; `verify` dispatches on `format` and never falls back. The v1 body and
domain are unchanged, so decisions from before M16b verify as they did. The executor, approval
service, recovery and both stores needed no change: they already verify through the signer, and
the stores keep decisions as JSON.

Matching gained an optional list that `rule_matches` appends to: one record per evaluated check
(rule id, path, operator, unreadable), written before the operator runs, so a check that raises
is the last one recorded. `unreadable(op, value)` implements the §5.1 table. The
`policy.decided` payload comes from `decided_payload(decision)`; a format 1 decision keeps the
old five keys, so recorded chains still match their decisions. Run views add `policy_checks`
(rule id, path, operator, boolean); the UI type gained the field but does not show it yet.

Settled within the plan:

- An unresolved variable or invalid operand stops evaluation at that check. The check is
  recorded with the value's readability, since the failure is in the policy input, not the value.
- The digest covers the rules' content, not the order of conditions inside a rule: canonical
  JSON sorts keys. Condition order changes the order of checks, never an outcome.
- Tests that seal decisions directly pass a fixed test digest; there is no way to issue format 1.

Limits: the checks explain a decision; they do not change one, and the M14 rule that unreadable
input never ends in allow stays the guarantee. The digest identifies the rule set, not the tenant
variables it was evaluated with.

S2 verification (`bench-results/m16b-s2-*.txt`):

```text
make check: ruff clean; mypy: no issues in 124 source files;
            2078 passed, 66 skipped in 174.08s
make test-integration (Postgres): 105 passed, 0 skipped
make ui-check: passed
make check-docs: checked 136 references, 0 problem(s)
python -m bench scripted: 38 records, 0 field differences from the S1 run;
            8 denied, 23 waiting for approval, 7 executed
python -m bench integrity: 23/23 match; every case line identical to S1
```

New tests are in `tests/unit/test_decision_format.py`: golden format 1 decisions (fixed key and
JSON, signed by the code before M16b) still verify; parsing and format rules; tampering with any
check field, the check list, the digest or the format fails; format 2 relabelled format 1 fails;
per-operator readability; checks stop where evaluation stops; RBAC and schema rejections carry
none; no value or operand reaches checks, the audit payload or the run view; a recorded pre-M16b
audit chain verifies and matches its decision; a run paused on a format 1 decision completes
after a restart, and one forged under another key is refused. A property test over M14's
generated inputs shows outcome, rule ids, reason and approver identical with and without check
recording. Postgres tests run a stored format 1 decision, and a pending format 1 approval, after
a restart; the run and step store round trips now carry both formats and checks.

### S3. Approval screen without model prose

`ToolManifest` gains `field_classes`: `typed` or `free_text` for every leaf path of the
execution schema. A leaf is a property whose schema is not a plain object with declared
properties, so arrays such as `delivery.mailboxes` are one field. Registration fails when the
declaration is missing, leaves a leaf undeclared or names a path that is not a leaf, in every
registered version, retired ones included. All shipped tools are declared: email `subject` and
`body` and the assessment `summary` are free text; everything else is typed. The declaration
order is the card order (recipients, amounts, ids, levels, record versions, end date).

The approval view, built from the approval's own snapshot, adds `typed_fields`,
`free_text_fields` (path, label, length in code points, and `content` kept apart) and
`difference`. The difference compares with the most recently decided approved approval for the
same tool name in the same tenant: typed fields as added, removed or changed with values; free
text only as changed or unchanged, by digest. The card shows typed fields, the action hash and
the difference first, then each free-text field in a closed `<details>` whose summary is the path,
the label and the length. The raw JSON block is gone from the card; `resolved_args` stays in the
API.

Settled within the plan:

- Values at paths the declaration does not cover are shown as free text, never as typed.
- In the difference, a field typed on only one side counts as free text, so no value is shown
  from a version where it was free text. An earlier version that is no longer registered counts as
  all free text.
- "Most recent" means decided most recently; a decided approval compares only with approvals
  decided before it, so re-reading it later gives the same answer. Rejected approvals never count.
- The approval stores gained `latest_approved`. Postgres joins approvals to steps and runs for
  the tenant and tool. The memory store asks the run and step stores through a callback that
  `memory_stores()` wires in; a bare memory store finds no earlier call.

Limits: the UI tests cannot render components (no DOM dependency), so the card model is tested as
a pure module and the card's source is checked for a closed `<details>`, the header, the order and
the absence of `resolved_args`. A typed field can still hold model-chosen text (a recipient
address); typing decides placement, not trust. Waiting: whether the wording and the collapsed
text change what approvers decide needs a study with real approvers.

S3 verification (`bench-results/m16b-s3-*.txt`):

```text
make check: ruff clean; mypy: no issues in 125 source files;
            2100 passed, 67 skipped in 185.65s
make test-integration (Postgres): 106 passed, 0 skipped
make ui-check: typecheck, 27 tests and build passed
make check-docs: checked 136 references, 0 problem(s)
python -m bench scripted: 38 records, 0 field differences from the S2 run;
            8 denied, 23 waiting for approval, 7 executed
python -m bench integrity: 23/23 match; every case line identical to S2
```

The first Postgres run failed one new test: approvals made by other tests in the same tenant
counted as later calls. The test now uses tenants of its own; the code did not change.

New tests: `tests/unit/test_approval_view.py` (classification of every shipped version,
registration failures, leaves, uncovered values, the one-sided-type rule, and through the API
the field split, code-point length, no earlier call, the difference, same tool and approved only,
tenant isolation, and a stable answer for a decided approval);
`tests/contract/test_postgres_concurrency.py::test_latest_approved_call_is_per_tenant_and_tool`;
`ui/tests/approval-view.test.ts` (order, labels, lengths, headers without field text, the
difference rows, and the card source check).

### S4. Grants expire, enforced by the sweep

Three layers, each tested on its own:

1. **The system's own rule.** The IAM mock reports a grant `expired` at or after its
   `expires_at`, with no sweep involved. Revocation is a separate record keyed by the grant
   (`grant_revocations`), written with `INSERT OR IGNORE`, so revoking twice records once and a
   revocation that lands before a delayed grant still applies to it.
2. **The sweep.** `VerificationResolver.prepare` adds a `grant_expiry` job before the write's
   read-back intent, so a dispatched grant always has one. Its id is deterministic, so a retry
   adds nothing. The job is due at `expires_at`; its operation key is the revocation's own key.
   The handler revokes, reads the grant back, and finishes only when it reads `revoked`
   (outcome `revoked`) or `absent` (outcome `not_granted`). Anything else leaves the job waiting.
   Bookkeeping follows the read-back resolver's order: save the result, audit `grant.revoked` on
   the grant's run and keep the event's sequence, refresh the anchor if the run has ended, then
   mark the job continued. The run's status does not matter, so a cancelled run's grant is
   revoked too.
3. **The alarm.** The grant-expiry heartbeat carries `overdue_active`: grants past their end date
   that nothing revoked, read from the IAM system rather than from the jobs, so a grant whose job
   is missing counts. `assess` raises `overdue_active` when the last two successful heartbeats
   both report a count above zero; one alone may just precede the sweep.

Renewal is a new run. There is no extend operation.

Settled within the plan:

- Bundles declare `revokers` per (tool, version). Startup fails unless every non-retired write
  whose execution schema requires `expires_at` has one, and only those.
- The job's deadline is the end date plus one day. Past it, the job still retries every tick; by
  then the alarm is firing.
- Jobs are now told apart by kind wherever a run's jobs are listed: read-back acknowledgment,
  reconciliation, the API's verification view and resume all ignore expiry jobs.
- Heartbeat outcomes gain `revoked` and `not_granted`; read-back heartbeats carry
  `overdue_active` as null. No SQL migration: jobs are stored by kind in existing columns.

Visible change: an IT access run's sweep now emits two heartbeats per tick (read-back and grant
expiry). A grant run's job list includes its expiry job.

Grants made before M16b have no end date. They never expire in the mock, are not counted as
overdue, and are not backfilled; an operator has to review them. The architecture page says so.

Limits: revocation is mock-only; a real IAM needs a revoke call and a read that reflects it. Two
sweepers whose lease overlaps can each write a `grant.revoked` event (the same narrow window the
read-back resolver has); revocation itself stays single. The alarm is a health signal; nothing
pages anyone yet.

S4 verification (`bench-results/m16b-s4-*.txt`):

```text
make check: ruff clean; mypy: no issues in 127 source files;
            2118 passed, 69 skipped in 189.88s
make test-integration (Postgres): 108 passed, 0 skipped
make check-docs: checked 136 references, 0 problem(s)
python -m bench scripted: 38 records, 0 field differences from the S3 run;
            8 denied, 23 waiting for approval, 7 executed
python -m bench integrity: 23/23 match; every case line identical to S3
```

New tests are in `tests/unit/test_grant_expiry.py` (18): the job exists before the write;
nothing happens before the end date; the system's own expiry rule; revoke and read back with the
audit event and the refreshed anchor; a missing job and a revoke that does not read back both
keep the grant overdue and fire the alarm; the alarm rule; a restart, two sweepers, a crash after
the revoke, a crash after the result and an expired lease all revoke once; a cancelled run's
grant; a grant never accepted; a revocation before a delayed grant; grants from before M16b;
startup checks. Two of them were also checked by breaking the guard they cover. Postgres:
competing runner processes revoke once, and a crash after revoking is finished once by a new
process.

### S5. Next-day review with a keyed sample

Migration 0007 (SQL and Alembic):

- `maintenance_jobs.run_id` is nullable, but only for `daily_review` jobs; every other kind still
  names its run. The record check now treats a null comparison as a failure: before, a record
  whose `run_id` was null next to a non-null column passed, because a CHECK lets an unknown
  result through. The schema test that found this is kept.
- `review_items`, with insert and select for `gawe_app` and update limited to the status,
  verdict fields and event sequence; the app role cannot rewrite what an item found or delete one.
- An index on `audit_events (type, ts)`.

The runner gained a scheduling hook. Before each pass, the review handler adds one job per tenant
(tenants with a run) for each of the last seven UTC days, each due at 01:00 UTC the following
day. Every field of a job is fixed by the tenant and the day, so racing runners add it once and
days already scheduled add nothing. A sweep that was down for up to a week catches up on its
first tick. The review does what §9.1 describes:

- It verifies each run's chain and matches each stored decision to an audit event by rebuilding
  its payload.
- For decisions audited that day it checks the signature and recomputes the action hash.
- It re-evaluates automatic, executed actions under the current rules.
- It draws the sample.

Items are insert-once with deterministic ids; each gets one `review.flagged` or `review.sampled`
event on its run, and an ended run's anchor is refreshed. The review never changes a run, a step
or a decision.

The API adds `GET /v1/review-items` and `POST /v1/review-items/{id}/verdict` for the
`legal_reviewer` role, with no verdict on a run the reviewer started. Items carry ids and reasons
only, never argument values. The UI adds a Review page.

Settled within the plan:

- **Reasons.** Reasons gained `superseded`, `action_mismatch` and `rules_may_have_changed`, to
  keep the cases apart. Changing a decision's `reason` text is caught as `signature_invalid`, not
  `audit_mismatch`, because the audit payload does not carry the reason.
- **Superseded.** An event no stored decision matches is `superseded` when the run has a
  read-back result of `recipient_changed`, and `audit_mismatch` otherwise.
- **Sample candidates.** Bundles declare them (`review_sample`). Contract review's are
  `crm.write_assessment` ratings of "low" with no flagged clause.
- **The key.** `GAWE_REVIEW_SAMPLE_KEY` must be hex, at least 32 bytes, and must differ from the
  signing key; startup fails otherwise. Unset, the review still runs, records `sample: off` in
  the job's result and logs a warning. `make env` and `make env-fill` generate it.
- **Status values.** `status` takes `open`, `confirmed` or `missed_risk`, as §9.3 lists; the
  verdict is the status.
- **Heartbeat.** It reports `scheduled` per kind and the `reviewed` outcome; counts by reason go
  to telemetry and the job's result.

Visible change: every app's sweep now has a `daily_review` heartbeat as well. The first tick
after deploy schedules and reviews up to seven past days per tenant.

Added after the first review of S5, at the owner's request: the seven-day catch-up, replacing a
schedule that only covered the previous day.

Limits:

- A sweep down for more than seven days skips the days before that window.
- On first deploy, the last seven days are reviewed, including decisions from before M16b.
- Re-evaluation uses today's values, so `inputs_changed` cannot say which value moved.
- Retired tool versions are not re-evaluated.
- Two reviewers whose leases overlap can each write an item's event, the window already queued
  for the other sweeps.
- The miss rate stays waiting until there is volume.

S5 verification (`bench-results/m16b-s5-*.txt`):

```text
make check: ruff clean; mypy: no issues in 130 source files;
            2143 passed, 74 skipped in 187.47s
make test-schema: 97 checks passed
alembic: upgrade to 0007, downgrade to 0006 and upgrade again on a throwaway database
make test-integration (Postgres): 113 passed, 0 skipped
make ui-check: typecheck, 29 tests and build (with /review) passed
make check-docs: checked 136 references, 0 problem(s)
python -m bench scripted: 38 records, 0 field differences from the S4 run;
            8 denied, 23 waiting for approval, 7 executed
python -m bench integrity: 23/23 match; every case line identical to S4
```

The first Postgres run failed three M16a runner tests: their helper expected one heartbeat per
tick and now gets two. The helper picks the read-back heartbeat; the code did not change.

New tests: `tests/unit/test_daily_review.py` (24) covers:

- one job per tenant and day, racing runners, and days missed while no sweep ran;
- a tampered decision, signature, arguments and audit event;
- superseded versus unexplained events;
- `rules_changed`, `inputs_changed` and `rules_may_have_changed`;
- the sample: reproducible with the key, different with another, about 5% on 20,000 draws, and
  only for low ratings with nothing flagged;
- the key rules;
- items and events once, with the anchor refreshed;
- the review changing nothing;
- tenant isolation;
- verdicts, including separation of duties.

Three guards were also checked by breaking them: the signature check, the sample draw and the
seven-day window.

Also new:

- `tests/contract/test_review_postgres.py` (5): the store, a tenant-scoped job, the new queries,
  and a whole review with racing runners and verdicts;
- `tests/api/test_http_review.py`;
- `tests/sql/test_review.sql`;
- `ui/tests/review-view.test.ts`.

### S6. Cumulative verification and completion record

**Scope.** The design and plan were committed first, then S1–S5, each after the owner's review.
The owner made all of these commits; the assistant made none. No other repository was touched, and the roadmap was not edited.
Approve-once (D5) is not built: it is split into its own milestone.

**Compatibility.**

- Format 1 decisions still verify and authorize, including after a restart, on memory and
  Postgres stores.
- A pre-M16b audit chain still verifies and matches its decision.
- Retired 1.0.0 writes fail closed if they never ran, and verify against their earlier records if
  they were sent.
- Migration 0007 upgrades, downgrades and upgrades again under Alembic.

**Changed expectations.** One existing test changed its expected outcome: a grant to an unknown
account now fails in preparation (`user_not_found`) instead of being denied. A new test keeps the
external-account rule. The other test changes only adapted helpers to the new tool versions, job
kinds and heartbeats. The only generated file in the diff is the recorded pre-M16b chain fixture.

**Privacy.**

- Policy checks carry rule ids, paths, operators and booleans.
- Heartbeats carry counts.
- Review items and their audit events carry ids, the day, the reason and the sample key's id.
- The cumulative review found that the `grant.revoked` audit payload carried the grant's
  `expires_at`, an argument value. It was removed. Two tests now pin the payload fields of the
  revocation and review events and check that no argument value appears in them.

**Negative matrix**, on an exported copy (`bench-results/m16b-s6-negative-matrix.txt`). Each
guard was broken in turn, a named test failed, and the file was restored. The intact copy passed
first.

| Guard broken | Test that failed |
|---|---|
| Revision comparison (record replaced under the same id) | `test_a_record_replaced_between_approval_and_execution_is_refused` |
| Guard inside the write's transaction (change between read and write) | `test_a_change_between_a_separate_read_and_the_write_is_refused` |
| Format 1 verification (old decision no longer verifies) | `test_golden_v1_decisions_still_verify` |
| Format 1 audit payload (old audit event no longer matches) | `test_recorded_pre_m16b_chain_still_verifies_and_matches_its_decision` |
| Checks and digest in the v2 signature (tampered v2 decision) | `test_changing_any_check_field_breaks_the_signature` |
| The system's expiry rule (expired grant still active) | `test_the_system_itself_treats_a_grant_as_inactive_at_its_end_date` |
| Read-back of the revocation (expired grant still active) | `test_a_revoke_that_does_not_read_back_keeps_the_job_and_the_alarm` |
| Overdue count (expired grant still active, unnoticed) | `test_a_missing_job_leaves_the_grant_overdue_and_the_alarm_fires` |
| Header without content (model text in a collapsed header) | UI: "a header contains none of the field's text" |
| Uncovered values as free text (model text shown as typed) | `test_values_the_declaration_does_not_cover_are_free_text` |
| Payload match in review (tampered decision missed by review) | `test_a_tampered_stored_decision_is_an_audit_mismatch` |

11 of 11 broken guards were caught.

Final verification (`bench-results/m16b-s6-*.txt`):

```text
make check: ruff clean; mypy: no issues in 130 source files;
            2145 passed, 74 skipped in 183.08s
make check-docs: checked 171 references, 0 problem(s)
make test-schema: 97 checks passed
alembic: upgrade to 0007, downgrade to 0006, upgrade again on a throwaway database
make test-integration (Postgres): 113 passed, 0 skipped
make ui-check: typecheck, 29 tests and build passed
python -m bench scripted: 38 of 38 records ok; 0 field differences from M16a's final run;
            8 denied, 23 waiting for approval, 7 executed
python -m bench integrity: 23/23 match; every case line identical to M16a's final run
```

Two checks first failed for this shell's environment, not the code: `docker` was not on PATH, and
the default `node` was a build for another CPU. They passed with Docker's own binary and Node
22.16.

The threat model gained T31–T35 and an updated T4. The results page and the API page describe
M16b. Mock-only: the revision check, revocation, the expiry rule and every write target.
Waiting: approver behaviour, the sample's miss rate and real trusted sources (§10).
