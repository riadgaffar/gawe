# M20 design: Action classes the engine only prepares

Status: **Done.** S1–S5 approved and committed by the owner and merged to `main` by fast-forward
(2026-11-22). D1–D5 approved as drafted on 2026-11-22. No paid runs. The plan and its progress
record are in [m20-plan.md](m20-plan.md); results are in the build record at the end and in
[results.md](results.md#m20-update-actions-gawe-only-prepares).

## 1. Purpose and boundaries

Some users keep execution to themselves for good: they want the agent to do the work up to the
last step and stop, every time, however well it has behaved. Better safeguards don't imply
consent to automate. Today GAWE has only "a person approves, the engine executes". M20 adds the
other one: **"the engine prepares, a person executes"**.

For an action class configured that way, nothing may let the agent dispatch it: not a policy rule
that allows it, not a person's approval, not an earlier approval of the same action, not a retry,
a resumed run or a sweep, and not any reusable authorization added later. Preparing the action
(resolving its arguments, expanding recipients, binding record versions) stays available, and the
person gets exactly what the engine would have sent.

In scope: the setting, the engine's enforcement, the hand-off to a person, the record of what they
did, and tests that try every path to dispatch. Out of scope: verifying the person's own action in
the target system (a connector question), classes defined by argument values (v1 is per tool
version), and any real connector.

## 2. What the codebase does today

- The executor is "the only code path that runs a tool". Before dispatch it re-checks the action
  hash, the signed policy decision (refusing a deny), an approval for `needs_approval`, the tool
  and its schema, and claims the step.
- It trusts the decision stored when policy ran. A rule changed after an approval does not stop
  that approved action. So a prepare-only guarantee can't live in policy alone: it has to be
  checked against the current setting at the moment of dispatch.
- Tenant settings are constants in each app (`TENANT_VARIABLES`); there is no per-tenant store.
- Steps move pending → ready → policy_checked → awaiting_approval/approved → executing → … →
  succeeded/failed. Run and step statuses are constrained in the database, so new states need a
  migration.
- Preparation (record versions, expanded mailboxes) runs before policy and its result is inside
  the action hash.
- No approve-once or other reusable authorization exists yet (split out of M16).

## 3. Decisions for the owner

### D1. Where the setting lives and where it's enforced

| Option | How | Problem |
|---|---|---|
| **A. A deployment setting, enforced at the executor (recommended)** | Each app declares `prepare_only`: tool versions that are never dispatched. Startup refuses a setting that names an unknown tool. The executor refuses dispatch for any step whose tool is in the current setting, whatever the stored decision or approval says. Policy evaluation and plan validation also mark such steps, so the run never asks for an approval it can't use | A setting per app, not per tenant, until there is a tenant store |
| B. A new policy effect, `prepare_only` | A rule like the others, stronger than `needs_approval` | Rules are judged when the step is checked; an approval made under older rules would still dispatch. Fails the requirement on its own |
| C. Both A and B | Rules for argument-level classes, the setting for the guarantee | More to build now; argument-level classes have no asker yet |

Recommendation: A. The executor check is what makes the promise true; the earlier marks only make
the run honest about what will happen. A policy effect can come later for argument-level classes.

### D2. What the person gets, and what they record

| Option | Hand-off | Problem |
|---|---|---|
| **A. A hand-off item with the prepared action (recommended)** | The step stops at a new state, `prepared`, and an item goes to the inbox of the class's role: the typed fields first, model-written text collapsed (as the approval screen since M16b), the preparation record and the action hash. The person does it by hand in the target system, then marks it **done** (with an optional reference, say a ticket or envelope id) or **not done** | The engine can't confirm the person's own action; it records their word, by name |
| B. A person-triggered dispatch button | The person clicks, the engine dispatches | That is approval under another name: the engine executes. Declined |

Recommendation: A.

### D3. States and what follows

- Step: `prepared` (waiting for a person), then `done_by_person` or `not_done_by_person`. Both
  are terminal. Run: a new `waiting_for_person`, like `paused_for_approval`.
- A later step that depends on it waits for "done", fails as upstream on "not done". A later step
  that uses its output (`$ref`) fails: a person's action has no output the engine can trust.
- Migration 0009 for the new states.

### D4. Who may mark it

The class names its role (say `legal_reviewer` for signatures). The person who marks it must hold
the role and must not be the run's requester, the same separation of duties approvals use. Marks
are audited by user, hash and code, never arguments.

### D5. Default and demo

Nothing is prepare-only by default, so today's behaviour and the benchmarks don't change. Tests
configure it. The demo documents one example (sending for signature) and how to switch it on.

## 4. What changes, if approved

- Engine: the setting and its startup check; plan validation and policy evaluation marking
  prepare-only steps; the executor refusing them at dispatch against the current setting; the
  hand-off states and transitions; migration 0009.
- API: list hand-offs for a role, mark one done or not done, both in the run view and audit.
- UI: a hand-off card next to approvals, reusing the approval card's typed-first layout.
- Tests: a matrix of every path to dispatch, each refused for a prepare-only step: an allowing
  rule, an approval granted, an approval granted before the setting, a resumed run, recovery after
  a crash, the read-back and review sweeps, a retried step, a direct executor call, and the
  benchmark harness's honest approvals.
- Docs: threat model row, API docs, results page, roadmap, completion records.

## 5. Expected changes to existing numbers

None: nothing is prepare-only by default. The scripted benchmark and integrity suite stay as they
are. A test configuration with signatures prepare-only shows the change: every signature attack
then stops at `prepared`, whatever its policy outcome.

## 6. Limits

- The engine can't confirm the person did it, or did it as prepared; it records what they say.
- Per tool version only; "admin grants only" or "over 100,000 only" needs argument-level classes.
- The setting is per app until there is a tenant store.

## 7. Approval

Approved by the owner on 2026-11-22: D1–D5 as drafted and the name M20. No paid runs. The build
plan is in `docs/m20-plan.md`.

## Build record

| Decision | Proposed | Result |
|---|---|---|
| D1 | A per-app setting, enforced at the executor against the current setting | Done: `AppBundle.prepare_only`, checked at startup; the executor refuses before any decision or approval check. Policy marks the step (it goes to `prepared`, not to an approval); plan validation is unchanged, as the approved plan had it, since a prepared step never reaches an approval it can't use |
| D2 | A hand-off item; the person does it and marks done or not done | Done: `GET /v1/handoffs`, `POST /v1/handoffs/{run}/{step}`, and a card beside the approvals, typed fields first, model text collapsed |
| D3 | `prepared`, `done_by_person`, `not_done_by_person`; run `waiting_for_person` | Done, migration 0009 (one `handoff` record per step rather than four columns). Not done ends the run `denied` with `handoff_not_done`; a `$ref` to a hand-off fails with `handoff_has_no_output` |
| D4 | The class's role, not the requester | Done; also bound to the current action hash, marked once, audited by user with hash, role and the reference's length |
| D5 | Off by default; a test configuration and a demo | Done: both shipped apps prepare nothing; `signatures_by_hand` for the demo (section 5d); the scripted variant runs in tests only |

With signatures prepare-only, all 3 scripted and 5 hand-written signature attacks move from
"waits for approval" to `prepared`; nothing is sent and every other record is unchanged. Without
the setting, the scripted run is identical to M19's.

### S5. Verification

Review of the cumulative diff (38 files): the setting and its startup check, the executor guard,
the hand-off states and migration 0009, the orchestrator's hand-off and marking, the API, the UI
card, the benchmark label and the demo. Audit payloads carry ids, the hash, the role, the policy
outcome and the reference's length, never arguments or the reference. A `$ref` is always a
top-level argument (the plan validator), and a hand-off has no saved output, so a reference to one
fails closed even without the explicit check.

Negative matrix on exported copies, after a control run that passed: 6 of 6 caught.

| Mutation | Caught by |
|---|---|
| The executor guard removed | `test_an_approval_granted_before_the_setting_does_not_dispatch`, `test_a_direct_executor_call_with_a_valid_allow_is_refused` |
| The guard reads what is stored on the step, not the current setting | `test_an_approval_granted_before_the_setting_does_not_dispatch` |
| The requester may mark | the marking-rule tests, in the engine and over HTTP |
| A stale hash accepted (both checks) | the marking-rule tests, in the engine and over HTTP |
| A hand-off's output usable by `$ref` | `test_nothing_can_use_a_hand_offs_output` |
| The harness marks a prepared step as an honest person | the prepare-only scripted variant, scripted and hand-written |

```text
make check: ruff clean; mypy clean; 2490 passed, 77 skipped
make test-integration (Postgres): 118 passed
make test-schema: 108 checks
alembic on a throwaway database: 0008 -> 0009 -> 0008 -> 0009
make check-docs: checked 199 references, 0 problem(s)
make ui-check: 38 tests, build
python -m bench scripted: 38 records, identical to M19's last run and to S3's
python -m bench integrity: 23/23 match
```

Limits that stand:

- The engine records what the person says; it can't confirm they did it, or did it as prepared.
- Classes are per tool version and per app.
- The hand-off list reads at most 500 waiting runs across tenants before filtering by tenant.
- The UI has no component-rendering test (queue item 11); the hand-off card was walked through by
  hand in a browser once.
- The demo section hasn't been run with the real model.

