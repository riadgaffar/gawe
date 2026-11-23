# M23 build plan

Status: **Done.** S1–S4 approved and committed by the owner and merged to `main` by
fast-forward (2026-11-23). It implements D1–D3 in
[m23-design.md](m23-design.md), approved by the owner on 2026-11-23. No paid model calls.

## Working rules and verification

Work on a branch `credentials-controls`. Do not commit, stage or push; the owner reviews and
commits each checkpoint. No live model calls and no real connectors. No migration is planned; if
one turns out to be needed, it stops for the owner first.

Run S1–S4 in order and stop for owner review after each, with the diff summary, the verification
output (and a log in an ignored `bench-results/m23-sN-*.txt`), known limitations and a suggested
one-line commit message. If verification fails twice on the same step, stop and report. A change
to approved semantics needs a new owner decision; routine details may be settled here and
recorded.

At each checkpoint run `make check` and `make check-docs`, `python -m bench scripted` (identical
records, unless S2 moves an IT case, which is reported first) and `python -m bench integrity`
(23 of 23); `make test-integration` when a step touches code the Postgres tests exercise;
`make ui-check` if the UI types change.

## S1. Scoped credentials (D1)

- A credential: a principal name and a set of scopes (`contract:read`, `assessment:write`,
  `envelope:send`, `mail:send`, `user:read`, `grant:write`). Each mock system checks the caller's
  credential at the start of every operation, inside the system, and refuses an operation outside
  its scopes with `credential_scope` before reading or writing anything.
- Each app builds one credential per tool from the tool's manifest: a read tool gets only the
  read scope for what it reads; a write tool gets the scope for its declared writes. Tool code is
  handed a client bound to that credential, not the whole mock. Trusted parts (preparation,
  read-back, revocation, the next-day review) keep their own credentials, separate from any tool's.
- Startup refuses a read tool whose credential holds any write scope, and a write tool whose
  credential lacks the scope for one of its declared writes.

Tests: a conformance test like M22's: for every tool, try every operation outside its credential
with that credential and check the system refuses it and nothing changes; each tool still works
with its own; the startup refusals; the benchmarks unchanged.

Suggested commit: `feat(connectors): give each tool a credential scoped to what it does`

## S2. Controls the agent can't touch (D2)

- IT app: `tenant.control_systems` (`["crm"]`, because contract review's deal values live
  there) and a rule: a grant of `write` or `admin` on a control system needs `security_admin`.
- Tests that pin the controls inventory: the policy rules, sources, variables, verifiers,
  revokers, guarantees, conditions and the prepare-only setting are fixed once the service is
  built (frozen, and no API route or maintenance command takes them as input); no tool's
  declared writes name a GAWE control; the database role can't write them (there is no table for
  them).
- Remove the leftover M16b comment in the mock CLM, found while exploring.

Tests: an honest ticket asking for `write` on `crm` waits for security, and runs once approved;
`read` on `crm` and `write` on `wiki` still run automatically; the inventory tests above; the
scripted run (any change is reported first).

Suggested commit: `fix(it-access): grants that can change another app's controls need security`

## S3. The step trace (D3)

- `GET /v1/runs/{id}/steps/{step_id}/trace`, any user in the tenant: the step as planned (tool,
  version, its arguments' hash), its decision (outcome, rule ids, the action hash it's bound to,
  whether its signature verifies), its approval if any (id, decision, who, when, hash), its
  attempts (number, status, operation key), its condition checks (from the audit trail's types,
  by count), its hand-off, and its read-back job (outcome, code, hash), plus `gaps`: each link
  that's missing or doesn't match the step's current action hash, named by code.
- Never argument values, model text or the read-back evidence's contents; hashes, ids and codes
  only, as the audit trail.

Tests, one run per outcome, each with an empty `gaps` list: success; policy denial; an approval
rejected; an unknown outcome; a refused condition; a hand-off marked done. Plus: a planted
mismatch (an approval bound to another hash, a read-back job for another hash) is named in
`gaps`; another tenant's run is not found; nothing in the trace carries an argument value.

Suggested commit: `feat(api): trace a step from proposal to result`

## S4. Verification and completion record

- Review the cumulative diff for scope, compatibility and privacy.
- Negative matrix on copies, each with a test that fails when the guard is removed, after a
  control run: a mock not checking the credential; a read tool given a write scope at startup; the
  control-system rule removed; the trace not reporting a mismatched hash.
- Every check above and both benchmarks.
- Docs: threat model (credential scope, control systems, audit links), API docs (the trace),
  roadmap (M23 row, queue item 8 done), both completion records.

Suggested commit: `docs: record verified M23 results`

## Approval and progress record

- Phase 2: design D1–D3 and the name M23 approved by the owner on 2026-11-23.
- Phase 3: this plan, approved by the owner on 2026-11-23.
- S1: built on `credentials-controls`, waiting for owner review.
  - `gawe.connectors.credentials`: `Credential` (principal and scopes), `CredentialScopeError`
    (`credential_scope`, a pre-dispatch rejection, so nothing is sent), and `ScopedClient`. Each
    mock declares `OPERATIONS` (method to scope) and checks the caller's credential itself
    (`authorize`) before anything else. The client exposes every operation of its system, so an
    out-of-scope call reaches the system and is refused there; it exposes nothing else.
  - Scopes: `contract:read`, `assessment:write`, `envelope:send`, `mail:send`, `user:read`,
    `grant:write`. Each app's `TOOL_SCOPES` gives each tool only what it does; `tool_clients`
    builds each tool's client (principal `gawe-tool:<tool>`), and the tool code uses only that.
    Preparation, read-back, revocation and the review keep using the systems directly, as trusted
    code, never a tool's client.
  - `AppBundle.tool_scopes` and `write_scopes`. Startup refuses a current tool with no
    credential, a read tool holding any non-read scope, a write tool lacking the scope of one of
    its declared writes or holding a change scope beyond them, and a write no scope names.
    Settled here: a write tool may also hold read scopes. The test app in `tests/api_bundle.py`
    declares scopes too; its fake connector doesn't enforce them.
  - Done early: the leftover M16b comment in the mock CLM is removed (planned for S2).
  - Tests: 12 in `tests/unit/test_credentials.py` (every tool has its own credential; each tool's
    credential refused by the system on every operation outside it, nothing changed; read tools
    hold no change scope; a client offers only the system's operations; the startup refusals;
    both apps start).
  - `make check` 2582 passed, 79 skipped; `make test-integration` 121 passed; scripted 38 of 38
    identical; integrity 23 of 23.
- S1: approved by the owner and committed.
- S2: built; approved by the owner and committed.
  - IT app: `tenant.control_systems` (`["crm"]`) and the rule `control-system-change-needs-security`:
    `write` or `admin` on a control system needs `security_admin`. `read` on `crm` and any level
    on other systems are unchanged. A tenant setting without `control_systems` makes every grant
    deny (fail closed, as with any unreadable policy variable).
  - **Finding, reported first:** one of M18's honest IT tickets, `sam_crm_write` (Sam fixes the
    ticket-to-CRM field mappings by hand), asks for exactly this and now waits for security.
    The clean set's expectation moved from `no_person` to `approval_required`, so its balance is
    5 and 7 instead of 6 and 6, and M18's "33 of 43 honest documents needed no person" would be
    32 if rerun. `lee_crm_admin` already waited (admin) and now matches this rule too. The scripted
    benchmark and the integrity suite are unchanged.
  - Tests pinning the controls inventory, in `tests/unit/test_controls.py`: the exact set of API
    routes (none takes or changes a control; a new one fails the test until reviewed); the two
    maintenance commands; the app bundle frozen; no tool writes a `config.*`, `run.*` or `gawe.*`
    resource; every system holding a policy source of contract review is a control system of the
    IT app (the IAM's name for it kept by hand); a missing setting denies; and end to end, `write`
    and `admin` on `crm` wait and run once approved, `read` on `crm` and `write` on `wiki` run.
  - The adversarial suite knows the rule: the grant oracle, the per-rule boundary registry and the
    unreadable-variable targets. Test fixtures that spell out the IT tenant settings gained
    `control_systems`; the golden policy cases now cover `crm` at three levels and `wiki` write.
  - `make check` 2601 passed, 79 skipped; `make test-integration` 121 passed; scripted 38 of 38
    identical; integrity 23 of 23.
- S3: built; approved by the owner and committed.
  - `GET /v1/runs/{id}/steps/{step_id}/trace` (`gawe.api.trace`), any user in the tenant; another
    tenant's run or a step not in the run's plan is not found. It shows the step (tool, version,
    status, the hash of its resolved arguments, its action hash), its decision (outcome, rules,
    the hash it's bound to, whether its signature verifies, whether its `policy.decided` event is
    in the audit trail), its approvals, its attempts (with the hash each operation key derives
    from), its condition checks by result and count, its hand-off, its read-back jobs, and `gaps`.
  - Settled here: the audit trail keeps only payload hashes, so the trace rebuilds each expected
    payload from the stored records and looks its hash up. That ties condition checks, the
    decision and each approval's request and (a person's) decision to this step, not only to the
    run, and a missing event is a gap.
  - Settled here: a recipient change (M16a) is the one legitimate way a step's hash changes. A
    hash is superseded only when a record of that change exists (a read-back job or an attempt
    ending `recipient_changed`); records bound to it are shown, marked not current, and are not
    gaps. Any other hash is a gap.
  - Gap codes: `decision_missing`, `decision_for_other_step`, `decision_hash_mismatch`,
    `decision_signature_invalid`, `decision_not_audited`, `approval_hash_mismatch`,
    `approval_not_audited`, `approval_missing`, `attempt_key_mismatch`,
    `dispatched_after_denial`, `dispatched_without_approval`, `dispatched_after_handoff`,
    `condition_not_checked`, `handoff_missing`, `readback_hash_mismatch`,
    `readback_plan_mismatch`, `readback_key_mismatch`, `readback_missing`.
  - Tests: 15 in `tests/unit/test_step_trace.py`, one run per outcome with no gaps (success,
    denial, rejection, unknown, a refused condition, a hand-off marked done, and a recipient
    change superseding an earlier hash); planted mismatches named (an approval and a read-back
    job for another hash, a missing read-back, a decision for another hash, an attempt keyed by
    another hash, a missing audit event); the tenant boundary; no argument or preparation value
    in the trace. One Postgres test (an approved write, no gaps), since rebuilt payloads must
    survive the database. The routes test lists the new route.
  - `make check` 2616 passed, 79 skipped; `make test-integration` 122 passed; scripted 38 of 38
    identical; integrity 23 of 23.
- S3 follow-up, at the owner's request (approved and committed): ten gap codes had no test that produced them. Each now
  has a planted break that the trace must name (a missing decision, one for another step, a
  dispatch after a denial, without an approval or after a hand-off, a missing condition check, a
  missing hand-off, a read-back job of another plan or key, a missing approval), and a test fails
  if a code in `gawe.api.trace` is never planted. `gawe.api.trace` is fully covered.
  `make check` 2627 passed, 80 skipped (the new Postgres test skips without a database).
- S4: verified; approved by the owner and committed. Cumulative diff reviewed; negative matrix 5 of 5 caught
  after a passing control run (log in `bench-results/m23-s4-matrix.txt`); every check and both
  benchmarks pass (`bench-results/m23-s4-check.txt`). Docs: threat model T40–T42 and the
  assumption about real connectors, API docs (tool credentials and control systems; tracing a
  step), roadmap (M23 row, queue item 8 done, the Next line, known limits), and the build record
  in [m23-design.md](m23-design.md).
