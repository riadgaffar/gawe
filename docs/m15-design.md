# M15 design: Policy edge cases

Status: **done — approved scope implemented and verified** (2026-11-12); S1–S4 complete.
Phase 0 scope and Phase 1 findings are recorded here. S1–S4 were committed by the owner;
the owner closed M15 on 2026-11-12.
The build plan is in [m15-plan.md](m15-plan.md), approved by the owner at the separate Phase 3 gate.

## 1. Outcome and scope

The model proposes; deterministic code decides. M15 makes individual policy boundaries explicit,
prevents a Planner from granting access to a different person, and detects policy dependencies
on state an earlier tool action could change. Work stays on `m15-policy-edge-cases`; the owner
commits after each reviewed step. No model calls or live paid benchmarks are needed.

Proposed delivery order: (1) per-rule boundary fuzzing, (2) reported-requester grants,
(3) variable provenance checks and sequence fuzzing. Item (4), audit-backed combination rules,
is split into a separate milestone by the owner's decision recorded in section 7.
Cross-workflow combinations are **waiting** for a shared subject key from a system of record;
IT access currently carries no contract/deal identity. Other policy changes are outside scope.

## 2. Independent boundary tests and email semantics

Each generated case carries independently specified arguments, variables, expected outcome,
expected matched rule ids, and expected parse status (`true`, `false`, or not applicable).
Do not derive expected values from `email_domain`, `apply_operator`, or `rule_matches`.
Use small, labelled generators: each kind has a fixed interpretation and varies its literal
contents. For numeric predicates, use direct arithmetic around a named threshold; for email,
construct a known local part/domain or a known malformed form. Use curated Unicode/punycode
pairs as literals, not the production normalizer, to obtain expected domain identities.

Call the real parser only as the system under test to compare with the parse label. Independently
assert the final signed engine decision, expected outcome and expected rule-id set. An early
schema/RBAC rejection has an explicit engine rule id, not a fabricated policy hit. Test overlapping
rules as well as isolated boundaries; include empty/nonempty variables and missing/wrong types.
A coverage check maps every shipped rule id to a written boundary and generated case family,
including the example fixture and the new requester rule. New rule ids require new coverage.

Use deterministic Hypothesis settings and an explicit fixed seed for the committed suite; no
persistent example database. Keep the number of examples bounded in `make check`. Do not add a
randomized deep-run command unless needed; any such command is separate from the required checks.
Retain M14's shipped-policy and operator-level unreadability properties, adjusting expected labels
only for approved semantic changes. Valid controls prevent tightening from becoming blanket denial.

### 2.1 Proposed address contract (owner decision)

One plain mailbox only. Reject lists, display names, quoted mailbox syntax, control characters,
whitespace anywhere, empty domain labels, leading/trailing hyphens in labels, trailing dots and
malformed encodings. Consume the entire input; never trim it into an allowed address. Preserve
plus-addresses and case-insensitive domain matching. Subdomains are distinct from their parents.
This is a policy comparison parser, not a promise of mail delivery or full mailbox RFC support.

Support international domains through an explicit IDNA normalization contract, applied identically
to input domains and configured domain operands, producing lowercase ASCII labels. Propose strict
IDNA2008 validation with a maintained library, no compatibility mapping that collapses look-alikes
onto an internal domain. ASCII case folding is permitted; reject unsupported encodings rather
than guessing. Dependency and normalization settings must be pinned in tests during implementation.
Preserve current Unicode local parts where valid under the plain-mailbox contract; do not add
provider-specific aliasing or local-part rewriting. No generic visual-similarity denylist.

| Case kind | Parses? | Contract Review email | IT grant domain gate |
|---|---|---|---|
| Exact configured internal domain, including plus-address or uppercase domain | yes | allow | allow if requester and other gates agree |
| Unicode domain and its curated ASCII encoding, explicitly configured internal | yes | allow | allow if requester and other gates agree |
| Valid subdomain or international domain not configured internal | yes | legal review | deny |
| Valid Cyrillic look-alike of an internal domain, not configured internal | yes | legal review | deny |
| Exact configured blocked domain | yes | deny | external-domain deny if outside internal list |
| Lists, display names, trailing dot/newline, malformed labels | no | legal review | deny |

An international domain does not become internal just because it parses. International allow
controls use test tenant configuration containing that domain; no shipped tenant allowlist expands.
If internal and blocked lists overlap, deny wins. Invalid configured domain entries should cause
a policy evaluation error when resolved, never be silently dropped from the operand list.

### 2.2 One-sentence boundaries (tested after schema/RBAC checks)

| Rule id (app or fixture) | Boundary |
|---|---|
| `read-tools-allowed` (Contract Review, example), `lookup-allowed` (IT) | Read tools have a baseline allow irrespective of writable-tool rules. |
| `crm-assessment-allowed` / `crm-write-allowed` | The named CRM write has a baseline allow, subject to every stricter matching rule. |
| `email-baseline-allowed` (Contract Review, example) | The named email tool has a baseline allow, subject to external-recipient and blocked-domain rules. |
| `external-email-needs-legal` (Contract Review, example) | A recipient whose parsed domain is not exactly in the internal list, or cannot be parsed, needs legal review. |
| `blocked-domain-email-denied` / `deny-competitor-email` | A parsed recipient on the blocked list is denied, including when another rule would allow or review it. |
| `low-risk-contradicted-needs-legal` | A proposed level in the run's unsupported-level list needs legal review; an empty list matches no level. |
| `low-risk-large-deal-needs-legal` | A low rating for a contract in the trusted large-deal list needs legal review; either false conjunct prevents this match. |
| `signature-standard-needs-legal` (Contract Review, example) | A valid deal value at or below 100,000 needs legal review. |
| `signature-high-value-needs-finance` (Contract Review, example) | A valid deal value above 100,000 needs finance approval. |
| `grant-baseline-allowed` | The grant tool has a baseline allow, subject to all stricter access rules. |
| `elevated-access-needs-security` | Admin access needs security approval. |
| `production-database-needs-security` | Access to prod_db needs security approval, regardless of level. |
| `external-user-denied` | A grant to a domain outside the internal list, including an unreadable address, is denied. |
| `requester-mismatch-denied` (proposed) | A grant to any mailbox other than the Reader-reported requester is denied. |

Boundary families include numeric threshold neighbors, enum members/nonmembers, empty lists,
missing operands, wrong JSON types, selector mismatches and overlapping predicates. Table rows
sharing semantics still have separately checked app/rule coverage. Deal-value list construction
gets its own threshold test; the policy rule itself sees contract ids, not the deal-value source.

## 3. Grants limited to the reported requester (owner decision)

Recommend **deny** for mismatch: granting to another person contradicts this request's scope.
Expose `request.requester_email` from the persisted Reader findings for the current run; no lookup
through a tool-written grant record. Add a deny rule on `iam.grant_access.email` using `ne` against
that variable. Missing, empty, non-string or unparseable reported requester must fail closed: omit
an unusable variable so evaluation produces `engine.error`, never a permissive default.

Recommend exact string equality for this milestone, matching the stated request. Case-only
changes or alternative encodings are mismatches even if a downstream directory treats them alike;
this conservative behavior is explicit, not silently normalized. Valid uppercase, plus-address
and international controls use the identical requester in findings and arguments. If account
identity normalization is wanted instead, choose it now and specify it independently of domain
classification; do not strip plus tags or assume aliases refer to the same person.

A fooled Reader can still name the attacker's account; this rule stops a fooled Planner only.
Change the three lateral-grant planner-mode expectations from `none` to `policy_deny`; leave their
reader-mode outcomes `none`. Keep prod_admin at approval and clean requests completing. The goal's
single isolated `policy` expectation must get an explicit reported-requester context (mismatch
for the lateral goal); never rewrite a reader-mode expected outcome just to match the new rule.
Expected aggregate, subject to verification: 7 attacker runs execute, 8 deny, 23 await approval
(out of the same 38 trials). This is an intentional benchmark change requiring approval here.

## 4. Policy-variable provenance and sequence tripwire (owner decision)

### 4.1 Declarative provenance with fail-closed startup checks

Recommend explicit declarations, rather than a bare `trusted: true` boolean. Add a write-resource
set to each tool manifest, distinguishing an explicit empty set from a missing declaration.
At app assembly every registered version must declare its writes, including read tools declaring
none. Use app-owned canonical resource names and dot-separated field scopes, not filesystem
paths or arbitrary globs. Parent and descendant scopes overlap; `crm` overlaps `crm.assessment`.
Unknown/wildcard writes cannot be treated as empty. A read-class tool declaring writes is invalid.

Each app declares a source catalog and a policy-variable dependency graph. Leaf sources identify
configuration, frozen per-run findings, or a named system resource; derived variables explicitly
name their dependencies. Resolve transitive dependencies, rejecting unknown names, cycles, empty
provenance and any overlap with a registered tool's writes. Require a declaration for every `$...`
operand in the policy, including checks that happen not to match in current sample runs. Validate
all declared policy-variable definitions, so adding a YAML reference later cannot expose an
already unsafe variable. Missing tool metadata cannot opt out of validation.

| Variable | Declared source | Relevant tool writes |
|---|---|---|
| `tenant.internal_domains`, `tenant.blocked_domains` | fixed tenant configuration | none |
| `review.unsupported_levels` | current run's frozen Reader findings | none |
| `review.large_deals` | `clm.contract.deal_value` | CRM writes `crm.assessment`, not CLM deal values |
| `request.requester_email` | current run's frozen Reader findings | IAM writes `iam.grant`, not findings |

Other write resources are `email.sent` and `esign.envelope`; lookup tools explicitly write none.
Use field-level declarations only where the connector implementation actually preserves that
separation. Changing a tool to write CLM deal values must reject the existing large-deal dependency.

The app supplies values through its existing callback, paired with these declarations. This is
validation of trusted application contracts, **not static proof of arbitrary Python data flow**.
A callback or connector lying about its dependencies can evade metadata validation; test source
adapters against real mock writes and document this trust boundary. Reader findings are stable
against tools, not necessarily true or immune to prompt injection. No claim of Reader truth is
introduced by labelling their source non-tool-writable.

Add rejection tests for missing declarations, all-version write overlap, broad/narrow scope
intersection, undeclared variables, derived-variable transitive overlap and cycles. A deliberately
misdeclared writable dependency is rejected at assembly; undeclared implementation behavior is
instead exercised by the sequence tripwire. Keep source declarations out of model prompts.

### 4.2 Sequence fuzzing that actually shares state

The existing benchmark creates a fresh bundle and stores inside every `drive()` call. Running
unrelated preliminaries before that call would test nothing. Refactor the harness narrowly to
allow reuse of one bundle, stores and mock-system state for a generated prefix and its attack.
Default CLI behavior remains isolated per trial. Do not change production connector behavior.

For every scripted (case, mode), generate bounded sequences (initially 0–6 actions, fixed seed,
10 examples per trial) of honest allowed actions through the real service/executor. Include
read tools, internal email, unconstrained CRM ratings and routine grants to that run's requester.
Check each selected action is allowed and completes; do not silently filter out blocked actions.
Retain the same mock objects and ledger when evaluating the subsequent attack. Recreate that
shared environment for each property example and each trial to prevent cross-example leakage.
Exercise within-run prefixes where practical as well as prior runs sharing the bundle.

Recompute the attack's variables after the prefix, and assert all referenced non-tool-writable
values for the same fixed findings are unchanged. Compare attack results to explicit suite
expectations for every goal/mode. Prefix effects must not count as attacker success: snapshot the
ledger offset and classify only the suffix, retaining prefix state. Prefixes use distinct action
ids/idempotency keys. Test the harness with a deliberately stateful fixture whose allowed write
changes a later gate, proving the tripwire detects it rather than always testing fresh state.

Run `python -m bench scripted` at verification and compare every result to its approved expectation;
a successful exit alone is not evidence that all expectations matched. Record the path, commit
and dirty status of the results rather than implying uncommitted code has a clean commit identity.

## 5. Within-app combination rules: recommend a separate milestone

This is larger than one additional rule. The audit store persists only `payload_hash`, not a
queryable payload; adding a subject key only to `AuditLog.record(payload=...)` loses the key.
The API reads events per run, with no tenant/app/subject history lookup. The CRM mock appends
assessment records but does not expose a trusted prior risk classification. A "second downgrade"
therefore needs an agreed event meaning as well as a private subject key.

Recommend splitting this item into its own separately approved milestone. Do not assign it an
existing milestone number or implement partial history metadata here. Candidate design for that
milestone, **not approval to build**:

- Define scope as same tenant, same app and same stable contract identity across runs. Distinguish
  "repeated low assessment" from a real decrease relative to an established previous rating;
  the latter needs a trustworthy baseline and explicit high/medium/low ordering. Decide whether
  the first unobserved rating counts. A model cannot choose the subject namespace or hash.
- Use a separate environment-supplied random HMAC key, at least 32 bytes, with an explicit key id
  and domain-separated canonical input containing tenant, app, subject type and subject id.
  Never use a plain hash or reuse the policy signing key. Keep raw ids out of audit metadata.
- Key rotation needs retained lookup keys or an authenticated migration; discarding old keys must
  not erase earlier history and silently permit a repeated downgrade. Missing keys or unavailable
  history hold the action for review/deny, never relax the baseline decision. No dev fallback key.
- Persist only minimal typed history metadata (subject digest, key id, event kind, classification,
  stable action identity), cryptographically bound to its audit event. Version event hashing and
  preserve old-chain verification. Provide an authenticated tenant-scoped index/projection backed
  by audit events, not a tool-writable CRM record or an unauthenticated cache. Existing unrecorded
  history must be explicitly unknown, not treated as proof of no previous downgrade.
- Record successful effects once, including recovery after crash/retry; proposed or denied actions
  are not completed downgrades. Atomically coordinate metadata with completion or make reconciliation
  fail closed. Per-subject concurrency must prevent two simultaneous actions both seeing zero
  history; per-run chain locks are insufficient.
- Enforce history as a separate monotone escalation stage: final outcome is never less restrictive
  than the history-free decision. Do not expose history variables to allow rules or to arbitrary
  guards that can suppress an existing denial/review. Avoid mixing approver roles silently; preserve
  conflict-deny semantics. Approvals bind the action and the history snapshot/version so replay or
  a changed history cannot erase the new review requirement.

This touches key management, migrations, audit event compatibility, persistence, recovery,
concurrency and policy evaluation. It exceeds the risk and implementation breadth of the other
items together. If retained in M15, first expand this section into a concrete approved design for
those contracts; do not put a partial implementation into the build plan.

## 6. Phase 1 findings and related limits

- `rules.py` currently accepts `a@acme.example` followed by a newline as internal; it also accepts
  `a@a..example` and `a@label-.example`. Unicode `a@bücher.example` is unreadable while its ASCII
  encoding parses. Plus-addressing and uppercase domains already work. These were reproduced
  locally without changes; item 1 should fix and regression-test the approved boundaries.
- `RuleEngine` evaluates every applicable rule, selects the strictest effect, records matched ids,
  validates arguments first and signs the result. Conflicting approval roles deny. Tests must
  account for all matching rules rather than assuming only the strongest id is recorded.
- `it_access._variables` currently returns tenant settings only. The mock IAM lowercases lookup
  keys, while policy equality is exact; section 3 makes the proposed conservative choice explicit.
- Contract Review derives contradiction lists from findings and large-deal lists from
  `MockCLM.deal_values()`. `write_assessment` writes the ledger, not the seeded contract map.
  Current IAM grants likewise write a ledger, not the source user map.
- `ToolManifest` has side-effect classification but no write-resource declarations. `AppBundle`
  has an arbitrary variables callback and no provenance schema. Neither proves dependency safety.
- `bench/injection/scripted.py` defines 38 trials in two modes; cases.yaml holds eight goals.
  `drive()` constructs fresh state and `classify()` receives all ledger effects. Both matter for
  sequence-test validity. Existing result matchers use policy operators, so new boundary oracles
  must not reuse those matchers for expected parser behavior.
- `AuditLog.record` computes a payload hash and persists only the event. Its introductory comment
  mentions an artifact store, but this method and the Postgres audit adapter do not persist/retrieve
  payload bodies. Do not assume queryable history already exists.
- M14 now includes additional operator and schema-bypass properties; keep them alongside the new
  per-rule tests rather than replacing them. The existing baseline is the current main branch.
- No AGENTS.md was found in the checked ancestors or repository file inventory. Working tree was
  clean on main before creating the M15 branch.
- Metadata cannot detect malicious trusted application code or external systems whose writers are
  not declared. Real-source provenance, mailbox forwarding, record-version approvals and audit
  unreadable markers require their own work. List these limits; do not fold them into this change.

## 7. Decisions approved (2026-11-12)

The owner approved all four recommendations:

1. Strict plain-mailbox parsing, explicit international-domain normalization and exact domain
   membership; look-alikes are external (email review, IT deny), with malformed values retaining
   those restrictive outcomes.
2. Deny requester mismatches using exact reported-email equality; fail closed on invalid/missing
   requester; change the three lateral-grant planner-mode benchmark outcomes to denial.
3. Explicit tool-write and variable-source declarations, transitive overlap checks and shared-state
   sequence tests, with the trusted-application-code limitation stated in section 4.
4. Split audit-backed combination rules into a separately designed milestone; cross-workflow
   combinations remain waiting for a shared system-of-record subject key.

Sections 2–5 retain the reasoning and alternatives behind these approved decisions. The history
proposal is context for the later milestone, not authorization to implement it now. The build plan
was approved separately before S1 implementation.

## Build record

S1 completed 2026-11-12. The parser now consumes the whole mailbox, rejects whitespace/control
characters, validates every domain label with strict IDNA2008, and canonicalizes configured
operands identically. Settings are explicit (`strict=True`, `uts46=False`); only ASCII domain case
is folded. `idna>=3.20,<4` is a direct runtime dependency, verified with installed version 3.20.
No shipped tenant allowlist or policy YAML changed. Invalid literal domain operands fail loading;
invalid runtime domain operands cause `engine.error`, including a mixed list containing a valid
match and an invalid entry. The parser remains a policy comparison parser, not full mailbox RFC
validation or proof that a mailbox exists.

Added 191 cases in `tests/adversarial/test_policy_boundaries.py`, with a fixed seed and deterministic
Hypothesis settings. Their expectations use case-kind labels, curated Unicode/ASCII literals and
simple threshold arithmetic; parser output never selects the expected outcome. The tests check
parse status, exact matched rule-id sets and signatures, cover each shipped rule, and preserve
legitimate plus-address, uppercase and explicitly configured international controls. Existing M14
operator/schema-bypass properties pass unchanged. Added threat-model row T21 with actual test links.

Verification output (full transcript saved locally in `bench-results/m15-s1-check.txt`):

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 97 source files
pytest --cov=gawe --cov-report=term-missing
1513 passed, 39 skipped in 64.84s (0:01:04)
python scripts/check_threat_model.py
checked 78 references, 0 problem(s)
```

`git diff --check` passed. S1 is left uncommitted for owner review; S2 has not begun.

S2 completed 2026-11-12. IT Access now exposes the exact requester from persisted findings and
rejects grant mismatches with `requester-mismatch-denied`. Unusable requester findings produce
no requester variable, so grants deny with `engine.error`. The parser checks usability only;
the comparison string is never normalized. Tests cover invalid/missing reports, case and alias
changes, international controls, run isolation, the fooled Planner and the remaining fooled-Reader
case. All existing admin/production and external-domain gates remain active.

Only the three lateral-grant planner outcomes changed. Scripted output at
`bench-results/20261112T213839Z-scripted` was checked record by record against all 38 expectations:
7 attacker steps ran, 8 denied, 23 awaited approval. The manifest records commit
`b672033ad06ddb5ab18743a85c12c9ee1c8046eb` and `git_dirty: true`; no model calls or spend.
The isolated lateral-goal policy case now explicitly uses the clean Reader's reported requester;
reader-mode lateral grants still run. Results documentation preserves the historical M13 table
and adds a dated M15 update. Threat-model row T22 records both the control and its Reader limit.

Full verification output is in `bench-results/m15-s2-check.txt`:

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 97 source files
pytest --cov=gawe --cov-report=term-missing
1540 passed, 39 skipped in 65.21s (0:01:05)
python scripts/check_threat_model.py
checked 83 references, 0 problem(s)
```

`git diff --check` passed. S2 is uncommitted for owner review. No design departures; S3–S4 pending.


S3 implementation completed 2026-11-12. `ToolManifest.writes` distinguishes absent declarations
(`None`) from explicit no writes (`()`). `AppBundle.policy_sources` uses typed `PolicySource`
leaves (configuration, findings, resource), and `policy_variables` uses typed `PolicyVariable`
dependency tuples. Source and variable names are disjoint; names and resource scopes use lowercase
ASCII dotted identifiers with digits/underscores after each segment's first letter. Parent and
child scopes overlap; sibling fields do not. Every source kind participates in overlap checks.

`load_app_rules()` checks tool schemas and provenance for all registered versions before service
assembly. The live-smoke entry point uses the same loader before constructing a provider. Pure
rule grammar/schema tests retain the lower-level loader. Both apps and assembly fixtures declare
sources and writes; no policy outcomes or variable callbacks changed. Tool write declarations stay
out of the Planner catalog. Resource scopes describe application data, excluding engine-owned
execution/audit bookkeeping. This validates trusted contracts; it cannot infer Python data flow
or prove the Reader's report true.

The provenance suite covers absent/empty metadata, malformed names, read/write contradictions,
missing variable declarations, unused unsafe definitions, unknown dependencies, cycles, empty
provenance, transitive overlap in both directions, safe siblings and unsafe older tool versions.
A CLM deal-value write rejects `review.large_deals`. Real mock CRM, email, e-sign and IAM writes
leave callbacks unchanged with fixed findings. Threat-model row T23 records the trust limit.
No design departures. S4 shared-state sequence testing remains pending.

The first full S3 check found one legacy API fixture reconstructing `AppBundle` without copying
its new declarations (1 failed, 1612 passed, 39 skipped). It now uses `dataclasses.replace` to
preserve app metadata while changing the Reader profile; the missing-tool fixture uses the same
pattern. Initial output is retained in `bench-results/m15-s3-check-initial.txt`.

Final S3 verification (full output in `bench-results/m15-s3-check.txt`):

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 98 source files
pytest --cov=gawe --cov-report=term-missing
1613 passed, 39 skipped in 64.41s (0:01:04)
python scripts/check_threat_model.py
checked 88 references, 0 problem(s)
```

Added 73 provenance cases; `git diff --check` passed. S3 is uncommitted for owner review.
S4 remains pending; audit-backed combinations remain split out and cross-workflow joins waiting.


S4 implementation: the benchmark accepts an optional `TrialEnvironment` containing the same
bundle/tool closures, stores, signer and ledger path across prefix and attack. Default `drive()`
and CLI calls still create fresh environments. Classification snapshots the ledger length before
each attack and inspects only new effects, preserving all earlier state. A regression executes a
legitimate grant matching a later attack goal and proves it is excluded from attack success.

Sequence tests cover all 38 scripted case/mode pairs after a previous run's allowed prefix, plus
all 26 planner cases with allowed prefixes inside the attack run. Reader-mode within-run prefixes
are omitted because the run has already received corrupted findings; those 12 cases are covered
with honest prefixes in prior runs. Each parameterized property uses 10 generated examples,
fixed seeds (15004/15005), no example database, and explicit empty/all-six-action controls.
Prefixes contain 0–6 actions: reads, medium/high CRM assessments, internal email, or routine grants
to the honest reported requester. Every selected action must allow and succeed through the real
service/executor. There is no filtering of blocked prefixes or approval bypass. Repeated actions
have distinct step/idempotency keys. Each example creates fresh state, then preserves it throughout
its prefix and attack.

Callback snapshots use the same fixed findings and deep copies, including an observation at the
within-run prefix boundary. The deliberately misdeclared fixture changes a large-deal policy
variable after an allowed CRM write: startup accepts its false metadata, but both source stability
and attack-outcome assertions detect the weakened gate. This demonstrates the reason to retain
both startup contracts and runtime sequence tests. T23 links the tests and states their bounded
coverage; this does not prove arbitrary histories safe or Reader findings true.

The first S4 focused check failed because the zero-action control submitted an empty plan, which
the existing validator rejects. Work paused under the plan's two-failure rule (including the S3
fixture failure). The owner approved continuation; zero actions now skip the prefix run and the
run-count assertion accounts for that. No security semantics changed. The resumed focused checks
passed: 127 passed in 14.91s (`bench-results/m15-s4-focused.txt`).


S4 completed 2026-11-12 with 68 new test cases (64 parameterized sequence properties and four
harness/coverage regressions). Final verification output, retained in `bench-results/m15-s4-check.txt`:

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 98 source files
pytest --cov=gawe --cov-report=term-missing
1681 passed, 39 skipped in 100.61s (0:01:40)
python scripts/check_threat_model.py
checked 92 references, 0 problem(s)
```

Final scripted run: `bench-results/20261112T215545Z-scripted`. Every recorded case/mode pair
matched its approved expectation and unauthorized-execution flag: 7 executed, 8 denied, 23 waited
for approval (38 total). The manifest records commit `a6713939b273344873aa0409a72f4fa544946c9f`,
`git_dirty: true` and `model: null`. Command output and the record-by-record comparison are retained
in `bench-results/m15-s4-scripted.txt` and `bench-results/m15-s4-comparison.txt`. No paid or live model
calls were made. The final changes do not alter suite expectations.

Cumulative scope review confirmed only the approved lateral-grant planner outcomes changed from
the pre-M15 suite. Policy boundaries, requester binding, startup provenance and shared-state tests
are implemented. The diff contains no generated benchmark output, private handoff references in
these milestone documents, audit migrations or unrelated feature changes. `git diff --check`
passed. All four design decisions remain as approved: strict addresses, exact requester mismatch
denial, explicit provenance plus sequence tests, and splitting audit-backed within-app combinations
into a separate milestone. Cross-workflow joins remain waiting for a trusted shared subject key.
M15 is complete for the approved scope; the owner committed S4 and closed M15 on 2026-11-12.
