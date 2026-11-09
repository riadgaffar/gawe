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
| `POST /v1/runs` | any user | start a run: `{objective, profile, document?}`. Returns `201` with the new run; planning and execution continue in the background |
| `GET /v1/runs?limit=` | any user | the tenant's runs, newest first |
| `GET /v1/runs/{id}` | any user in tenant | run, steps, policy outcome per step |
| `POST /v1/runs/{id}/cancel` | starter or `admin` | cancel (409 if a step is executing) |
| `POST /v1/runs/{id}/resume` | starter or `admin` | drive the run again, e.g. after a restart; `202` |
| `GET /v1/runs/{id}/audit` | any user in tenant | audit events (type, actor, payload hash, chain hash) |
| `GET /v1/runs/{id}/audit/verify` | any user in tenant | recompute the hash chain and check it against its anchor, see below |
| `GET /v1/runs/{id}/events` | any user in tenant | server-sent events, see below |
| `GET /v1/approvals` | approver | open approvals this caller could decide now |
| `POST /v1/approvals/{id}/decision` | approver | `{decision: "approved"\|"rejected", action_hash, comment?}` |

An approval appears in the inbox only for someone holding the role the policy named, who did not
request it, in the same tenant, while its step is still waiting. The approver sees the tool, the
exact resolved arguments and the `action_hash`, and must send that hash back; a different hash is a
`409`.

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
Resolved tool arguments appear only in the approval view, because an approver has to see what they
are approving.

## Startup recovery

When the API starts it drives every run that a previous process left unfinished (`created`,
`planning`, `running`, `paused_for_approval`), oldest first, up to 200. This is safe to repeat: a
step is claimed once and tools receive an idempotency key. Set `GAWE_AUTO_RESUME=0` to turn it off.

## Failed sign-in limit

Failed authentications are counted per client address in memory. `GAWE_AUTH_FAIL_LIMIT` (default 20)
failures within `GAWE_AUTH_FAIL_WINDOW_SECONDS` (default 300) return 429 with `Retry-After` until the
window passes. Successful requests are not counted. The count resets when the process restarts, and
behind the UI all users share one address.
A run that ends has its open approvals closed (rejected by `gawe.system`, audit event
`approval.closed`), so none can be approved afterwards.
