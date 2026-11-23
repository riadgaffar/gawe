# GAWE HTTP API (v1)

Base path `/v1`. JSON in, JSON out. Interactive docs are served by FastAPI at `/docs` when the
server runs. Every endpoint except `/healthz` needs `Authorization: Bearer <token>`.

## Authentication

Tokens are random secrets issued out of band. The server stores only their SHA-256 hashes in the
file named by `GAWE_API_TOKENS_FILE`; each hash maps to a tenant, a user and roles. `make
dev-tokens` creates development tokens for the people in `scripts/seed_dev.sql`. This is a stub:
production puts an OIDC provider in front and reads roles from the database. The rest of the API
only ever sees an `Identity` (tenant, user, roles).

## Errors

Every error is `{"error": {"code": "...", "message": "..."}}`.

| Status | code | When |
|---|---|---|
| 401 | `unauthorized` | missing or invalid token (`WWW-Authenticate: Bearer`) |
| 403 | `forbidden` | right tenant, wrong role or not the owner; refused self-approval |
| 404 | `not_found` | no such run or approval, **or it belongs to another tenant** |
| 409 | `conflict` | approval already decided or expired, action hash mismatch, run busy |
| 429 | `too_many_requests` | too many failed sign-ins from this address; `Retry-After` says how long (default: 20 failures in 5 minutes) |
| 413 | `payload_too_large` | body over the size cap (default 1.5 MB) |
| 422 | `validation_error` / `bad_request` | malformed body (details list field and reason, never the values) |

## Endpoints

| Method and path | Who | Purpose |
|---|---|---|
| `GET /healthz` | anyone | liveness |
| `GET /v1/me` | any user | the caller's `user_id`, `tenant_id` and `roles` (used by the UI to check a token) |
| `GET /v1/tools` | any user | registered tools with input schemas |
| `POST /v1/runs` | any user | start a run: `{objective, profile, document?, source_id?}`. Returns `201` with the new run; planning and execution continue in the background. See [Runs started from a record](#runs-started-from-a-record) |
| `GET /v1/runs?limit=` | any user | the tenant's runs, newest first |
| `GET /v1/runs/{id}` | any user in tenant | run, steps, policy outcome and evaluated checks per step, each write step's verification record, and the conditions it is only checked against (`checked_not_enforced`, see [What targets guarantee](#what-targets-guarantee)) |
| `GET /v1/runs/{id}/verification` | any user in tenant | independent read-back records for the run, see [Independent write verification](#independent-write-verification) |
| `GET /v1/runs/{id}/steps/{step_id}/trace` | any user in tenant | one step's records joined by hash and key, with any broken link named, see [Tracing a step](#tracing-a-step) |
| `POST /v1/runs/{id}/cancel` | starter or `admin` | cancel (409 if a step is executing) |
| `POST /v1/runs/{id}/resume` | starter or `admin` | drive the run again, e.g. after a restart; `202` |
| `GET /v1/runs/{id}/audit` | any user in tenant | audit events (type, actor, payload hash, chain hash) |
| `GET /v1/runs/{id}/audit/verify` | any user in tenant | recompute the hash chain and check it against its anchor, see below |
| `GET /v1/runs/{id}/events` | any user in tenant | server-sent events, see below |
| `GET /v1/approvals` | approver | open approvals this caller could decide now |
| `POST /v1/approvals/{id}/decision` | approver | `{decision: "approved"\|"rejected", action_hash, comment?}` |
| `GET /v1/handoffs` | the role the setting names, not the requester | prepared actions this caller could mark now, see [Hand-offs](#hand-offs) |
| `POST /v1/handoffs/{run_id}/{step_id}` | the same | `{outcome: "done"\|"not_done", action_hash, reference?}`, once |
| `GET /v1/review-items?status=` | `legal_reviewer` | next-day review items in the tenant; `status` is `open` (default), `confirmed`, `missed_risk` or `all` |
| `POST /v1/review-items/{id}/verdict` | `legal_reviewer` who did not start the run | `{verdict: "confirmed"\|"missed_risk"}`, once; a second verdict is a `409` |

## Runs started from a record

A profile can read its document from a record in another system instead of an upload. IT Access
reads from the ticket system:

- `source_id` is required, and `document` is refused. The id is at most 64 characters of
  letters, digits, `.`, `_` and `-`.
- The server fetches the ticket's text in the caller's tenant. An unknown ticket, another
  tenant's ticket or an unreadable ticket system is a `422` with its code, and no run is created.
- A profile without a source refuses `source_id`.
- The run keeps `source_id` (also in `GET /v1/runs/{id}` and in the `run.created` audit event).
  The fetched text is cleared when the run ends, as an upload is.

Policy reads who filed the ticket and through which channel from the ticket system, not from its
text. Grant preparation reads the ticket again and refuses one that is no longer open
(`ticket_not_open`).

An approval appears in the inbox only for someone holding the role the policy named, who did not
request it, in the same tenant, while its step is still waiting. The approver sees the tool, the
exact resolved arguments and the `action_hash`, and must send that hash back; a different hash is a
`409`.

The approval view also splits the arguments the way the tool declares them:

- `typed_fields`: recipients, amounts, ids, levels, record versions and end dates, as path and
  value, in the tool's order.
- `free_text_fields`: text the model wrote, each with its path, the label "Written by the model
  from an untrusted document", its length in code points, and the text in a separate `content`
  field. The UI shows content only after an explicit expand.
- `difference`: the comparison with the most recently approved call of the same tool in the same
  tenant. Typed fields are listed as added, removed or changed with their values; free text only
  as changed or unchanged. `previous_approval_id` is null when there is no earlier approved call.
- `history`: for a tool whose app declares history (M24), the earlier actions on the same
  subject that the step's rules counted, oldest first, at most the latest 10: `tool`, `run_id`,
  `step_id`, `run_started_at` and `values`, the typed arguments the app names (in Contract
  Review, the earlier `risk_level`; in IT access, the earlier grant's `system` and `level`).
  Empty for other tools. The `history.read` audit event counts all of them, by reference only.

Values a tool does not declare are listed as free text. `resolved_args` stays in the view for
clients that need the raw arguments.

## Hand-offs

An app can make a tool version prepare-only (`AppBundle.prepare_only`, empty in both shipped
apps). GAWE then resolves, prepares and checks such a step as usual but never sends it: the step
stops at `prepared` and the run at `waiting_for_person`, and a person does it by hand in the target
system.

- `GET /v1/handoffs` lists prepared steps in the caller's tenant whose role the caller holds, on
  runs the caller didn't start. Each has the same `typed_fields` and `free_text_fields` as an
  approval (no `difference`), the `action_hash`, the `role`, the policy outcome and reason, and the
  preparation record.
- `POST /v1/handoffs/{run_id}/{step_id}` records what the person did: `done` (with an optional
  `reference`, up to 120 letters, digits, `.`, `_`, `:` and `-`, such as an envelope id) or
  `not_done`. The `action_hash` must be the one shown. Refusals carry a code as the message:
  `role_required` and `requester_cannot_mark` are `403`; `action_hash_mismatch` and
  `not_waiting_for_a_person` (already marked, cancelled or never prepared) are `409`;
  `reference_invalid` is `422`. Another tenant's run, or a step that was never handed off, is `404`.
- Done counts as success for later steps; not done ends the run as `denied` with
  `handoff_not_done`. Nothing may use a hand-off's output.
- The run view's step carries `handoff`: the role, who marked it, when, and the reference. The
  audit trail records the mark by user with the hash, role and the reference's length, never its
  text.

The engine records the person's word; it can't confirm what they did in the target system.

## What targets guarantee

Each app declares, per write tool version, what its target system guarantees, and startup
refuses a write that relies on something its target doesn't (M22):

| Guarantee | Meaning | Which writes need it |
|---|---|---|
| `idempotent_by_key` | The system records an operation once per GAWE's key | Every write |
| `authoritative_readback` | Its own record of the operation can be read by that key; labels or delayed history don't count | Every write |
| `conditional_write` | It checks the bound record's revision in the write's own transaction | A write that binds a record |
| `atomic_reservation` | It reserves against a running total in the write's own transaction | A write that a rule judges with a `counter` policy source |

A write that the app makes prepare-only needs none of them; GAWE never sends it.

Some conditions live in another system, so the target can't check them at the write. The IT app's
grants rely on the ticket still being open (`ticket_open`). Such a condition is read on every
attempt, immediately before the tool is called. If it no longer holds, nothing is sent and the
step fails with `condition_changed`; if it can't be read, `condition_unreadable`. Each check is
audited as `condition.checked` with the condition names and `held`, `changed:<name>` or
`unreadable:<name>`, never what was read. A run's steps list these conditions in
`checked_not_enforced`. The window between that read and the write remains: a ticket closed in
it still gets its grant, which then expires as usual.

## Tool credentials and control systems

Each app gives each tool a credential scoped to what the tool does (`tool_scopes`; M23): a read
tool only `<thing>:read` scopes, a write tool the scope of each resource it declares it writes
(`write_scopes`). The target system checks that credential before every operation and refuses
anything outside it with `credential_scope`, before reading or writing. Startup refuses a tool
with no credential, a read tool holding a scope that changes anything, and a write tool whose
credential lacks or exceeds the scopes of its declared writes.

The IT app's `tenant.control_systems` names the systems that hold another app's controls: `crm`,
where contract review's deal values live. Write or admin access to one waits for a
`security_admin` (`control-system-change-needs-security`); read access doesn't. A tenant setting
without the list denies every grant.

## Rules on earlier actions

An app can let rules read its own earlier actions on the same subject (`AppBundle.history`;
M24): earlier steps of a tool, any version, in the same tenant, naming the same subject argument,
read from GAWE's step records. Every step past policy counts except denied, rejected and not-done
ones; in the same run only earlier steps of the plan. A rule may use these only to add a review or
a denial: startup refuses a `history.*` variable in an `allow` rule.

- Contract Review keys on `contract_id`. A `risk_level` lower than the contract's standing rating
  (the highest of the last rating that took effect and every counted one after it) waits for a
  `legal_reviewer` (`rating-lowered-needs-legal`).
- IT access keys on the grantee's `email`, lower-cased. Tenant settings `automatic_grants` (2) and
  `history_days` (30): once a person has been granted a system `automatic_grants` times, at any
  level, in the `history_days` before the run started, the next grant waits for a
  `security_admin` (`repeated-grant-needs-security`). A missing or unusable setting denies every
  grant.

Each read writes a `history.read` audit event with the step, the tool, the count and each counted
step's run id, step id and action hash, never argument values; history that can't be read records
the error class and the step denies. The approval view lists the earlier actions (`history`,
above).

## Tracing a step

`GET /v1/runs/{id}/steps/{step_id}/trace` joins what GAWE stored about one step, by the hash or key
that links each record to it (M23). Another tenant's run, or a step not in the run's current plan,
is a `404`.

| Field | What it holds |
|---|---|
| `tool`, `tool_version`, `status`, `error` | the step |
| `args_hash`, `action_hash` | the hash of its resolved arguments, and its action hash |
| `superseded_hashes` | earlier action hashes a recorded recipient change replaced |
| `decision` | outcome, rule ids, approver role, the hash it's bound to, `signature_valid`, and `audited` (its `policy.decided` event is in the trail) |
| `approvals` | each approval: id, hash, `current` (bound to the current hash), role, who asked and when, the decision, who and when, `audited` |
| `attempts` | each attempt: number, status, operation key, the hash that key derives from (`null` when none), error code, output hash, times |
| `conditions` | the step's `condition.checked` events, counted by result |
| `handoff` | the hand-off, if the step is prepare-only |
| `readback` | each read-back job: id, operation key, plan version, hash, `current`, outcome and code |
| `gaps` | each link that's missing or doesn't match, by code; empty when every link holds |

The audit trail keeps only payload hashes, so the trace rebuilds the payload each record should
have produced and looks its hash up; a record with no matching event is a gap. A recipient change
(M16a) is the one legitimate way a step's hash changes: a hash is superseded only when an attempt
or read-back job for it ended `recipient_changed`. Records bound to a superseded hash are shown,
not current, and aren't gaps.

| Gap | Meaning |
|---|---|
| `decision_missing` | the step is past policy but has no decision |
| `decision_for_other_step`, `decision_hash_mismatch` | the decision names another step or another action |
| `decision_signature_invalid` | the decision's signature doesn't verify |
| `decision_not_audited`, `approval_not_audited` | a record with no matching audit event |
| `approval_missing` | the decision needs an approval and none is bound to the current hash |
| `approval_hash_mismatch` | an approval bound to a hash that is neither current nor superseded |
| `attempt_key_mismatch` | an attempt's operation key derives from no known hash |
| `dispatched_after_denial`, `dispatched_without_approval`, `dispatched_after_handoff` | an attempt for the current hash that its decision, approvals or hand-off didn't allow |
| `condition_not_checked` | dispatched, with conditions declared, and no check recorded |
| `handoff_missing` | the step is handed to a person but carries no hand-off |
| `readback_hash_mismatch`, `readback_plan_mismatch`, `readback_key_mismatch` | a read-back job for another action, plan version or operation key |
| `readback_missing` | a write that was dispatched with no read-back job for the current action |

The trace carries hashes, ids, codes and times only: never argument values, model text or read-back
evidence. It reads GAWE's own records, so it can't link what happened outside GAWE beyond what the
read-back saw.

## Verifying the audit trail

`GET /v1/runs/{id}/audit/verify` recomputes every hash in the run's chain. When the server keeps
anchors (`GAWE_ANCHOR_SINKS`), each finished run's last event is copied outside the database, and
the response also says how the chain compares with that copy:

```json
{
  "ok": true, "checked": 14, "head": "...", "failed_seq": null, "reason": null,
  "anchor": {
    "status": "matched", "source": "stored", "sink": "file", "external": false,
    "seq": 14, "anchored_at": "...", "timestamped_at": null, "events_after_anchor": 0
  }
}
```

| `anchor.status` | Meaning | `ok` |
|---|---|---|
| `none` | No anchor for this run (anchoring is off, or the run has not finished) | the chain decides |
| `matched` | The chain reaches the anchored event unchanged | the chain decides |
| `mismatch` | The chain is shorter than the anchor, or the event there has changed: history was truncated or rewritten | `false` |
| `evidence_invalid` | The stored anchor fails its own check (wrong run or tenant, altered digest, bad signature) | `false` |
| `unavailable` | The anchor could not be read or checked right now | the chain decides |

* `events_after_anchor` counts events added after the anchor, such as an approval decided just as
  the run ended. They are reported, not treated as a failure.
* `external` is `true` only when someone other than us vouches for the anchor: the `rfc3161` sink,
  whose signed timestamp from DigiCert or Sectigo is re-checked against pinned roots on every
  verify. `timestamped_at` is the authority's time. The `file` sink is not external.
* An auditor who kept their own copy of an event's position and hash can check against it instead:
  `?anchor_seq=14&anchor_head=<64 lowercase hex>`. Both are required together; the response then
  has `source: "caller"`. A hash that was sent is never echoed back.


`GET /v1/runs/{id}/events` returns `text/event-stream`:

```
id: 7
event: audit
data: {"seq":7,"type":"policy.decided","actor":"gawe.orchestrator","payload_hash":"...","hash":"...","ts":"..."}

event: end
data: {"status":"completed"}
```

* Events replay from the start. Reconnect with `Last-Event-ID: <seq>` (or `?after=<seq>`) to resume.
* `: keepalive` comments are sent while idle. The stream ends with `event: end` when the run is
  finished, or after one hour (reconnect to continue).
* Events carry hashes and ids only. Fetch `GET /v1/runs/{id}` for the current state.

## What responses never contain

Uploaded documents, Reader findings, prompts, model output, policy signatures, tool outputs.
Resolved tool arguments appear in full only in the approval and hand-off views, because an
approver has to see what they are approving and a person doing it by hand has to see what to do. Verification records (on the run view and at `/verification`) are the
one other place argument values appear: the bounded fields a read-back compares, such as accepted
email recipients, contract id, risk level, account, system, level, signer and deal value. Email
subject and body and CRM summaries appear only as digests. They are visible to users in the run's
tenant, like the run itself.

A step's `policy_checks` name the rule, the argument path, the operator and whether the value was
unreadable; never the value or the operand. Review items carry ids, the day, a reason and, for a
sample, the sample key's id; never argument values.

## Startup recovery

When the API starts it drives every run that a previous process left unfinished (`created`,
`planning`, `running`, `paused_for_approval`, `waiting_verification`), oldest first, up to 200.
This is safe to repeat: a step is claimed once and tools receive an idempotency key, and a write
with persisted intent is only ever read back, never sent again. Set `GAWE_AUTO_RESUME=0` to turn
it off. The verification sweep (below) starts with the API regardless and also finishes evidence
for runs that ended (for example, were cancelled) while a read was pending.

## Failed sign-in limit

Failed authentications are counted per client address in memory. `GAWE_AUTH_FAIL_LIMIT` (default 20)
failures within `GAWE_AUTH_FAIL_WINDOW_SECONDS` (default 300) return 429 with `Retry-After` until the
window passes. Successful requests are not counted. The count resets when the process restarts, and
behind the UI all users share one address.
A run that ends has its open approvals closed (rejected by `gawe.system`, audit event
`approval.closed`), so none can be approved afterwards.

## Independent write verification

`GET /v1/runs/{id}/verification` returns tenant-protected verification records, including the
plan/step identity, action hash, engine-derived operation key, original deadline, due time, outcome,
protocol error (when present), and bounded
expected/observed evidence. The same record appears on the corresponding step as `verification`.
Text such as email subject/body and CRM summaries is represented by digests. Audit events carry
only an evidence digest and status/code, not this structured evidence.

A write awaiting system visibility is `awaiting_readback`, and its run is `waiting_verification`.
No further work starts while the run waits. Independent disagreement is `verification_blocked`
and fails the run. Unresolved verification at the original five-minute deadline becomes
`verification_unknown`, with terminal run status `unknown`: the action may have occurred and is
not automatically repeated. Output-schema failures remain protocol failures even when read-back
confirms the effect. An unexpected returned record id cannot select the record to verify.

Explicit resume and startup recovery use the same read-only resolver. A dispatched operation can
be read after approval expires; expiry cannot authorize another write. Cancellation keeps pending
reconciliation and never resumes downstream work. The scheduled sweep below revisits delayed
observations without any request.

Verification records retain their original deadline and are selected by the current action hash
on each step, so a recipient refresh does not display an older action's evidence as current.
Explicit resume on a terminated run may finish pending evidence; it cannot restart its plan.
When anchoring is configured, post-terminal evidence extends the protected history with a linked
checkpoint after checking the existing protected prefix. Earlier checkpoints remain in the file;
a signed timestamp checkpoint needs a fresh token for its own digest.

## Scheduled verification sweep (operators)

The API process runs a maintenance runner through its lifespan: one tick at startup, then every
`GAWE_MAINTENANCE_INTERVAL_SECONDS` (default 30), whether or not requests arrive. Shutdown stops
scheduling, lets a tick in progress finish and releases the runner's thread. There are three job
kinds:

- `write_readback`: independent verification of each write, described below.
- `grant_expiry`: revokes a grant at its end date (apps that grant access).
- `daily_review`: each tenant's next-day review.

Each tick claims due jobs in pages of `GAWE_MAINTENANCE_BATCH_SIZE` (default 50, at most
500) up to `GAWE_MAINTENANCE_MAX_JOBS` (default 500, at most 10000) per tick, in due-time order.
A claim is a short lease taken by compare-and-set on the job's revision, so any number of API
processes or one-shot sweeps can run at once: a lost claim is counted as `contended`, and a
runner whose lease expired cannot overwrite a newer result. A job that fails goes to the back of
the queue for one interval, so it cannot starve later work.

`GAWE_VERIFICATION_DEADLINE_SECONDS` (default 300, from 30 to 3600, and longer than the interval)
is fixed into each write's intent before dispatch; changing it affects only later writes.

For each due job the runner reads through the same resolver as explicit resume and startup
recovery. Agreement succeeds and continues the run's existing plan through the normal executor
gates (policy, approvals, authorization for any new dispatch); disagreement blocks and fails the
run; the deadline turns unresolved verification into `verification_unknown` and the run into
`unknown`. In the API the continuation runs on the background worker pool, not on the sweep's
thread, so a slow next step cannot delay ticks or heartbeats; until it is acknowledged the job is
moved back one interval and handed over again, which is harmless because each run is driven by
one worker at a time. The one-shot sweep continues inline before it exits. The runner has no write path: a dispatched write is never sent again, including after
its approval expired while the result was invisible. Cancelled, failed, denied and unknown runs
are only reconciled, never resumed, and the runner never plans a run.

One pass without the API, against the configured Postgres stores:

```sh
make sweep        # = python -m gawe.maintenance sweep --once
```

It reads the API's settings (`GAWE_APP`, `GAWE_POLICY_SIGNING_KEY`, the database variables and
`GAWE_DEMO_DIR`, so the mock systems reopen the same state directory as the API). It refuses
`GAWE_STORE=memory`, because a separate process cannot see an in-memory store; the API's own
schedule covers memory mode. It constructs no model provider and plans nothing. It prints one JSON
heartbeat per job kind and exits 0 when all are ok, 1 when the tick failed and 2 on bad settings.

### Grant expiry

Every grant from `iam.grant_access@2.0.0` has an `expires_at`, from the tenant's grant lifetime
(default 7 days, 1 hour to 30 days). The IAM system treats it as inactive from then on. The
`grant_expiry` job is created before the grant is written and is due at `expires_at`. It revokes
the grant with its own operation key and finishes only when the IAM system reads the grant back as
revoked, or as never granted. It then audits `grant.revoked` on the grant's run and refreshes the
run's anchor if the run has ended. Grants from cancelled runs are revoked too. Renewal is a new
request; there is no extend.

### Grants made before M16b

Grants from `iam.grant_access@1.0.0` have no end date, and the IAM system has nowhere to keep one.
`python -m gawe.maintenance end-old-grants` gives each of them one, on the durable stores like
`sweep --once`:

- It finds them in GAWE's own records: each 1.0.0 grant step GAWE sent (succeeded, or with its
  outcome left unknown or blocked) and the execution that sent it, whose key is the key the IAM
  system knows the grant by. Grants GAWE didn't make are never touched.
- The end date is the later of when the grant was made plus the grant lifetime the app uses
  today, and now plus a notice period (`--notice-days`, 7 by default, 1 to 30). No old grant
  outlasts a new one made the same day, except by the notice.
- Without `--apply` it prints one JSON line per grant (tenant, run, step, when made, end date,
  `rule`: `lifetime` or `notice`, and whether its job exists) and a total, and writes nothing.
  It never prints the grant's email, system or level.
- With `--apply` it adds a `grant_expiry` job for each grant, due at its end date, and audits
  `grant.end_date_set` on the grant's run (step, grant key, end date, rule, notice days). Running
  it again adds nothing: a grant that has its job keeps the date it was given.
- From then on it's an ordinary expiry: the sweep revokes at the end date and finishes when the
  IAM system reads the grant back as revoked or never granted, then audits `grant.revoked`.
- The IAM system doesn't know these dates, so the expiry heartbeat's overdue count adds old-grant
  jobs past their end date and not done. A grant whose job is missing isn't counted.

Exit status 0, or 2 for a configuration error (memory stores, a notice outside 1 to 30, an app
that makes no grants).

### Next-day review

At 01:00 UTC each tenant's `daily_review` job reviews the previous UTC day. Each tick schedules
the last seven days, so a sweep that was down catches up. For each run with a policy decision
that day, the review:

- checks the audit chain;
- rebuilds each stored decision's audit payload and matches it to an event;
- verifies the signature and the action hash over the stored arguments;
- evaluates automatic actions that ran again under today's rules.

Findings are recorded as review items with a reason:

- `audit_mismatch`, `superseded`, `signature_invalid` or `action_mismatch` for tampering checks;
- `rules_changed`, `inputs_changed` or `rules_may_have_changed` for re-evaluation;
- `sampled_low` for the sample.

The sample draws 5% of automatic low ratings with nothing flagged, using HMAC-SHA256 under
`GAWE_REVIEW_SAMPLE_KEY` over the action hash. The key is hex, at least 32 bytes, and must differ
from the signing key; `make env` generates one. Without it the review still runs, with the sample
off, and the API logs a warning. Each item gets one `review.flagged` or `review.sampled` audit
event on its run. The review never changes a run or a decision.

### Upgrading to M16b

- **Apply migration 0007** (`make migrate`). It lets review jobs belong to a tenant instead of a
  run and adds `review_items` (insert and verdict updates for `gawe_app`, no deletes). It also
  indexes `audit_events (type, ts)`. Downgrading to 0006 removes the review data.
- **Set `GAWE_REVIEW_SAMPLE_KEY`** (`make env-fill` adds one to an existing `.env`).
- **Drain pending steps of the 1.0.0 writes.** Only `crm.write_assessment@2.0.0`,
  `esign.send_for_signature@2.0.0` and `iam.grant_access@2.0.0` execute. A planned 1.0.0 step that
  never ran fails with `unsupported_tool_version`; a 1.0.0 write already sent still verifies
  against its earlier record.
- **End grants made before M16b** with `end-old-grants` (see [Grants made before
  M16b](#grants-made-before-m16b)). Until it runs they have no end date and never expire.
- Decisions recorded before M16b still verify and still authorize; new decisions are signed in
  format 2 and add the evaluated checks and the rule-set digest.

### Upgrading to email v2 and read-back

- **Drain or cancel pending `email.send@1.0.0` steps first.** Only `email.send@2.0.0` executes.
  A planned v1 step that never ran fails closed with `unsupported_email_version`; an in-flight v1
  send fails with `legacy_send_reconciliation_required` for an operator to investigate. Neither is
  resent or marked verified, and old hashes and signatures are not rewritten. Finished runs stay
  readable.
- **Apply migrations 0005 and 0006** (`make migrate`). 0005 adds step preparation metadata, the
  step's signed decision and per-approval snapshots; 0006 adds the verification states and the
  `maintenance_jobs` table, with grants for `gawe_app`. Downgrading past 0006 is refused: old code
  cannot represent waiting or unknown verification, so it needs a reviewed data migration.
- **Keep the mock system-state directory** (`GAWE_DEMO_DIR`). The mock directory, mail server and
  write targets keep their own operation records there, separate from the application ledger;
  the API and `make sweep` must point at the same one.
- A write that was in flight under the previous version, without a persisted dispatch intent, is
  flagged for investigation rather than given an invented deadline or verified from its reply.

### Heartbeat and alarm

Every tick logs one record per job kind on the `gawe.maintenance` logger, message
`gawe.maintenance.heartbeat {json}`, with the same fields as a `heartbeat` attribute:

```json
{"event": "gawe.maintenance.heartbeat", "kind": "write_readback", "status": "ok",
 "started_at": "...", "finished_at": "...", "duration_seconds": 0.02, "checked": 3,
 "outcomes": {"done": 1, "blocked": 0, "newly_unknown": 1, "still_waiting": 1, "failed": 0,
              "refreshed": 0, "continued": 0, "pending_continuation": 0, "skipped": 0},
 "errors": 0, "contended": 0, "scheduled": 0, "unresolved_total": 4, "unknown_total": 7,
 "overdue_active": null, "error": null}
```

Outcomes also include `revoked` and `not_granted` (grant expiry) and `reviewed` (the daily
review). `scheduled` counts jobs the kind created this tick. `overdue_active` is reported for
grant expiry only: grants past their end date that nothing revoked, read from the IAM system
itself, so a grant whose job is missing is counted too.

`unresolved_total` counts every job still waiting for a usable read; `unknown_total` counts every
terminal unknown, including acknowledged ones, so it never silently drops. A failed tick (for
example, the database is unreachable) logs at ERROR with `"status": "error"`, the exception class
in `error` and null totals; it is never reported as a successful tick with zero work. Heartbeats
carry counts, timings and kinds only: no recipients, arguments, bodies, run or tenant ids.
The same signals go to telemetry: `gawe.maintenance.heartbeat` (count by kind/status),
`gawe.maintenance.jobs` (count by outcome), `gawe.maintenance.backlog` (observed per ok tick,
`state=waiting|unknown`) and `gawe.maintenance.tick_seconds`.

Three separate alarm conditions (`gawe.maintenance.runner.assess` implements all three):

* **Unknown growing**: `unknown_total` at the latest `ok` heartbeat is larger than at the `ok`
  heartbeat before it. Ticks are succeeding but writes are ending without evidence.
* **Heartbeat stale, missing or failing**: no heartbeat for a kind within a few intervals
  (for example, 90 seconds at the default), none at all, or the latest has `"status": "error"`.
  The scheduler has stopped or cannot reach its stores; zero work cannot be told apart from silence.
* **Overdue grants**: `overdue_active` is above zero in the latest two `ok` heartbeats of
  `grant_expiry`. A grant outlived its end date and was not revoked.

Example queries:

```sh
# the latest heartbeats from JSON-lines logs
grep 'gawe.maintenance.heartbeat' api.log | tail -3 | grep -o '{.*}' | jq '{finished_at, status, unknown_total}'
```

```sql
-- unknown and still-waiting verification, by tenant
SELECT tenant_id, record->'result'->>'outcome' AS outcome, count(*)
FROM maintenance_jobs
WHERE record->>'kind' = 'write_readback'
  AND (record->'result' IS NULL OR jsonb_typeof(record->'result') = 'null'
       OR record->'result'->>'outcome' = 'unknown')
GROUP BY 1, 2;

-- results not yet continued (should drain within a tick or two)
SELECT job_id, run_id, due_at FROM maintenance_jobs
WHERE NOT continued AND record->'result' IS NOT NULL
  AND jsonb_typeof(record->'result') <> 'null'
ORDER BY due_at;
```

Routing these conditions to a pager is outside GAWE.

### What a result means

A recipient change found before sending blocks the send: nothing reaches the mail server, and the
step returns to policy and a fresh approval. A mismatch found by read-back after acceptance
(`verification_blocked`) means the effect already happened: the extra recipient received the
message or the record holds other values. Blocking the step does not undo it. `unknown` means the
effect may have happened and was not repeated. Read-back proves the mock target's record of the
operation; real mail and directory behaviour (delivery, forwarding, list membership) is not checked.
