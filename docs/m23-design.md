# M23 design: Credentials and controls the agent can't touch

Status: **Done.** S1–S4 and the S3 follow-up approved and committed by the owner and merged to
`main` by fast-forward (2026-11-23); S4 verification recorded below. D1–D3 approved as drafted on 2026-11-23. No paid runs. The plan and
its progress record are in [m23-plan.md](m23-plan.md); results are in the build record at the end.

## 1. Purpose and boundaries

The second half of queue item 8 (M22 did the first). Three reader requirements:

- **Credentials that match the action.** Where the target supports it, a read credential can't
  write: try a write with it and check the target refuses. A tool's read or write label doesn't
  restrict its credential.
- **Controls the agent can't touch.** Tools the agent can call can't change the policies,
  verifiers or credential settings that constrain them, through any execution path.
- **Audit links.** Proposal, decision, approval, dispatch and observed result are linked for
  success, denial and unknown alike. Name the gaps in today's links rather than add an audit
  system.

In scope: modelling credentials in the mock connectors and testing their scope; an inventory of
GAWE's controls and every path to them, with the gap found below; a per-step trace that links a
step's records; tests. Out of scope: a real connector or identity provider, and generic shell or
Git controls (the reader's `--no-verify` examples are application-specific).

## 2. What the codebase does today

**Credentials.** None are modelled. Each tool implementation is handed the whole mock object,
so the code behind a read tool could call a write. The architecture says the model holds no
credentials, and the threat model assumes real connectors use scoped service accounts; nothing
tests either. GAWE's own secrets (signing key, review key, API tokens, database credentials) live
only in trusted processes, and the database role can't rewrite history (T14).

**Controls.** GAWE's controls are the policy rules, policy sources and variables, the read-back
adapters (verifiers), revokers, declared guarantees and dispatch conditions, the prepare-only
setting, tenant settings and the keys. All are loaded at startup from the app and environment;
no API route, maintenance command or tool changes them at runtime. Tools only call connectors.
Within one app, M15 refuses a tool whose writes overlap a policy source, and M22 refuses a write
whose target lacks a guarantee.

**A gap, found while exploring:** the IT app grants `write` on `crm` automatically to the person
who filed the ticket (only `admin` and `prod_db` need a security approver). In the mocks, that
system also holds the contract records whose deal value contract review's policy reads, to decide
whether a signature needs finance. So an honest-looking ticket gives someone, without a person
looking, the power to change a value that gates another app's approvals. The agent doesn't change
the value itself, but it grants the access that can. M15's overlap check can't see it: it's
within one app, and here the source and the grant are in different apps.

**Audit links.** A probe of a successful write shows `plan.created`, `policy.decided`,
`execution.started`, `execution.succeeded`, `verification.readback`, `run.completed`; a denial
stops at `step.denied`. Payloads are stored only as hashes (by design), so the trail alone can't
show which step an event was about. The links exist, but across stores: the planned step, its
signed decision bound to the action hash, the approval bound to the same hash, the attempts
keyed by an operation key derived from it, and the read-back job carrying it. Nothing assembles
them; a reviewer has to join four stores by hand.

## 3. Decisions for the owner

### D1. Credentials

| Option | How | Problem |
|---|---|---|
| **A. Scoped credentials in the mocks (recommended)** | Each mock system checks a credential on every call. Each tool gets its own, scoped to what its manifest says (a read tool: reads; a write tool: its declared writes). A conformance test, like M22's: with each tool's credential, try every operation outside its scope and check the system refuses, writing nothing | Every mock and both apps change; it proves the pattern on mocks only |
| B. Document the assumption | The threat model already assumes scoped service accounts | Leaves the reader's test undone |

### D2. Controls the agent can't touch

| Option | How | Problem |
|---|---|---|
| **A. Pin the inventory, and close the cross-app gap with a person (recommended)** | Tests that no route, command or tool path changes a control at runtime. The IT app names the systems that hold controls (`crm`, because contract review's deal values live there); a grant of `write` or `admin` on one waits for a security approver | Which systems hold controls is declared by hand; a real deployment must keep that list right |
| B. Deny such grants outright | Nobody gets write on those systems through GAWE | Blocks honest requests that a person would approve |
| C. Document only | | The gap stays open |

### D3. Audit links

| Option | How | Problem |
|---|---|---|
| **A. A step trace (recommended)** | `GET /v1/runs/{id}/steps/{step}/trace`: the step as planned, its decision (outcome, rules, the hash it's bound to), its approval if any (who, when, the hash), its attempts (operation key), condition checks, its hand-off, and its read-back outcome, each with the hash or key that links it, plus a `gaps` list naming any link that's missing or doesn't match. Tests for success, denial, rejection, an unknown outcome, a refused condition and a hand-off | One more endpoint and view |
| B. Document how to join the stores | No code | Nobody checks the links |

## 4. What changes, if approved

- Mocks: a credential check on every operation; per-tool credentials built by each app from the
  tool's manifest; a conformance test that each credential can't do anything outside its scope.
- IT app: a `tenant.control_systems` setting (`crm`) and a rule that `write` or `admin` on one
  needs `security_admin`. Tests that no route, maintenance command or tool path changes a control.
- API: the step trace, with its tests per outcome; API docs.
- Docs: threat model (credential scope, control systems, audit links), roadmap, completion
  records. The mock CLM's leftover M16b comment, found while exploring, is removed.

## 5. Expected changes to existing numbers

The scripted benchmark has no IT case for `write` on `crm`, so it should stay identical. If an
IT attack does ask for it, it moves from "ran" to "waits for approval"; that would be reported as
a finding. The integrity suite should stay at 23 of 23.

## 6. Limits

- Credentials are proved on mocks; a real connector's scopes are a claim to verify with the same
  test against its sandbox.
- The list of systems that hold controls is kept by hand.
- The trace reads GAWE's own records; it can't link what happened outside GAWE.

## 7. Approval

Approved by the owner on 2026-11-23: D1–D3 as drafted, the name M23, and no paid runs. The build
plan is in `docs/m23-plan.md`.

## Build record

| Decision | Proposed | Result |
|---|---|---|
| D1 | Scoped credentials in the mocks | Done: `gawe.connectors.credentials`. Each tool's code gets a `ScopedClient` bound to its own credential (`gawe-tool:<tool>`); the system's own `authorize` runs before every operation and refuses anything outside the credential (`credential_scope`) before reading or writing. Each app's `tool_scopes` and `write_scopes`; startup refuses a missing credential, a read tool holding a change scope, and a write tool lacking or exceeding its declared writes' scopes |
| D2 | Pin the inventory; a person for the cross-app gap | Done: tests pin the routes, the two maintenance commands, the frozen bundle and that no tool writes a GAWE control. The IT app's `tenant.control_systems` (`crm`) and `control-system-change-needs-security`; a missing setting denies every grant |
| D3 | A step trace | Done: `GET /v1/runs/{id}/steps/{step_id}/trace` with 18 gap codes, each produced by a test. Audit events are matched by rebuilding their payload hashes; only a recorded recipient change supersedes a hash |

The gap found while exploring is closed: write or admin on the CRM now waits for a security
approver. That moved one honest benchmark ticket (`sam_crm_write`) from "no person" to "needs
approval"; M18's "33 of 43 honest documents needed no person" would be 32 if rerun. Scripted and
integrity are unchanged.

A wording point for readers of D1: the credential check is the system's own method, which the
client calls before each operation; the operation methods don't call it themselves. Tool code
never holds the raw system, so the client is the only path, and a mock whose check is removed is
caught by the conformance test (below).

### S4. Verification

Review of the cumulative diff (30 files): credentials and the mocks' operation maps, the startup
check, both apps' scopes and clients, the control-systems setting and rule, the trace module,
route, view and wiring, tests and docs. `AppBundle`'s new fields have defaults, but a bundle
without `tool_scopes` no longer starts: every current tool must name its credential (fail closed,
as intended). The trace and the audit carry no argument values; `ApiService`'s new parameters are
optional.

Negative matrix on exported copies of `HEAD`, after a control run that passed (319 passed,
1 skipped): 5 of 5 caught.

| Mutation | Caught by |
|---|---|
| The mock IAM doesn't check the credential | `test_a_tools_credential_is_refused_by_the_system_outside_its_scope` |
| A read tool (`iam.lookup_user`) given a write scope | startup refuses the IT app, so `test_both_shipped_apps_start_with_their_credentials` and every IT test fail, as does `test_read_tools_hold_no_scope_that_changes_anything` |
| Startup allows a read tool a change scope | `test_startup_refuses_a_credential_that_does_more_or_less` |
| The control-system rule removed | the controls end-to-end and fail-closed tests, the golden cases, the adversarial boundary registry and grant oracle, and the clean benchmark case |
| The trace ignores an approval bound to another hash | `test_an_approval_bound_to_another_hash_is_a_gap` |

```text
make check: ruff clean; mypy clean; 2627 passed, 80 skipped
make test-integration (Postgres): 122 passed
make ui-check: 39 tests, build
make check-docs: checked 232 references, 0 problem(s)
python -m bench scripted: 38 records, identical to S2's run (and M20's)
python -m bench integrity: 23/23 match
make test-schema, alembic: not run; no schema change
```

Limits that stand:

- Credentials are proved on mocks; a real connector's scopes are a claim to verify with the same
  test against its sandbox. Trusted code (preparation, read-back, revocation, the review) keeps
  its own access and isn't scoped per tool.
- The list of systems that hold controls, and the IAM's name for each, is kept by hand.
- The trace reads GAWE's own records; it can't link what happened outside GAWE beyond what the
  read-back saw, and nothing calls it automatically.
