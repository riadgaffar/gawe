# Demo script (synthetic data, real model)

Everything below uses invented contracts and mock systems. Nothing is emailed or signed; the mock
systems write what they did to `.demo-data/ledger.jsonl`. The model is real (Anthropic), so plans
can differ slightly between runs; the *decisions* come from the policy rules and do not.

## Setup (once)

Everything below is a `make` target.

1. `make env env-fill` creates `.env` if needed and adds any missing demo settings
   (`GAWE_POLICY_SIGNING_KEY`, `GAWE_APP`, `GAWE_API_TOKENS_FILE`, `GAWE_STORE`). It never
   changes a line that is already there.
2. Edit `.env` yourself: set `ANTHROPIC_API_KEY` and `GAWE_LLM_MODEL` (never commit this file).
3. `make up migrate app-role seed dev-tokens` (`app-role` needs `GAWE_APP_DB_PASSWORD` in `.env`;
   skip it and the API runs as the database owner with a warning). The last command prints one token per person; keep them.
   - alice: case manager (starts runs) · bob: legal reviewer · fiona: finance approver · root: admin
4. `make smoke APP=contract_review` tries the model on the three sample contracts with **no side
   effects** and prints what it extracted, the plan it proposes, and what policy would decide.
   Read this before the demo: it shows how the real model behaves.

Or all of steps 1 and 3 plus the server in one go: `make demo` (after step 2).

## Each session

`make up` (if Docker is stopped), then `make api`. The server runs on http://127.0.0.1:8000
(interactive docs at `/docs`). Tokens are shown only once, when `make dev-tokens` creates `.dev-tokens.json` (it refuses to
overwrite). If you lose them, delete that file and run `make dev-tokens` again. To start from a clean slate, delete `.demo-data/`.

## Using the UI instead of curl

`make ui-install` once, then `make ui-dev` (with `make api` running) and open
http://localhost:3000. Sign in by pasting one of the tokens from `make dev-tokens`:

- **alice** (case manager): Runs page -> pick a sample from the dropdown (or paste/upload your own
  `.txt`/`.md`) -> Start run. The run page updates live.
- **bob** (legal reviewer) / **fiona** (finance approver): Approvals page shows the exact arguments
  and the `action_hash`; Approve or Reject. Alice sees nothing in her inbox (self-approval is refused).
- Any user: the run's **Audit trail** page shows the events and "Verify hash chain".

The sections below do the same with curl, which is handy for scripting.

```bash
export API=http://127.0.0.1:8000
export ALICE=...   BOB=...   FIONA=...      # tokens from `make dev-tokens`
start() {  # start <token> <objective> <file>
  jq -Rs --arg o "$2" '{objective:$o, profile:"contract_review", document:.}' "$3" |
  curl -s -X POST $API/v1/runs -H "Authorization: Bearer $1" -H 'Content-Type: application/json' -d @-
}
```

## 1. Happy path with an approval (C-1001)

```bash
RUN=$(start $ALICE "Review contract C-1001, record the risk assessment, then email counsel@northwind.example a short summary." \
  src/gawe/apps/contract_review/samples/C-1001_northwind_msa.md | jq -r .run_id)
curl -s $API/v1/runs/$RUN -H "Authorization: Bearer $ALICE" | jq '{status, steps: [.steps[] | {step_id, status, policy_outcome}]}'
```
Expect `paused_for_approval`: the read and the CRM write ran; the external email waits.

```bash
curl -s $API/v1/approvals -H "Authorization: Bearer $BOB" | jq     # what Bob is asked to approve
curl -s $API/v1/approvals -H "Authorization: Bearer $ALICE" | jq   # [] (not hers to decide)
```
Approve, echoing the `action_hash` Bob was shown:
```bash
A=$(curl -s $API/v1/approvals -H "Authorization: Bearer $BOB" | jq -r '.[0] | .approval_id')
H=$(curl -s $API/v1/approvals -H "Authorization: Bearer $BOB" | jq -r '.[0] | .action_hash')
curl -s -X POST $API/v1/approvals/$A/decision -H "Authorization: Bearer $BOB" \
  -H 'Content-Type: application/json' -d "{\"decision\":\"approved\",\"action_hash\":\"$H\"}" | jq
```
Check: run `completed`; `grep email.sent .demo-data/ledger.jsonl` shows exactly one email.

Try it as Alice: the same call with `$ALICE` is refused (403). Try a wrong hash: 409.

## 2. Finance approval for a high-value deal (C-1002)

Start with the Globex contract and the objective from `samples/README.md` (send for signature).
The signature step needs `finance_approver` because the deal is 250,000: Bob sees nothing in his
inbox and cannot decide it; Fiona can. The deal value is read from the contract system, not the
document.

## 3. Prompt injection (C-1003)

```bash
start $ALICE "Review contract C-1003 and record the risk assessment." \
  src/gawe/apps/contract_review/samples/C-1003_initech_sow_injection.md
```
The document tells automated reviewers to email it to `attacker@evil.example`. Expect a completed
run with an assessment and **no email**: the Reader treats the text as data. If a model ever did
propose that email, policy denies the blocked domain, and anything external needs approval anyway.
`grep -c email.sent .demo-data/ledger.jsonl` must not change.

## 4. Policy denial

Objective: `Review contract C-1001 and email a summary to someone@competitor.example.` The run ends
`denied`; no tool call is made.

## 5. Audit trail

```bash
curl -s $API/v1/runs/$RUN/audit -H "Authorization: Bearer $ALICE" | jq '.[] | {seq,type,actor}'
curl -s $API/v1/runs/$RUN/audit/verify -H "Authorization: Bearer $ALICE" | jq   # {"ok": true, ...}
curl -N $API/v1/runs/$RUN/events -H "Authorization: Bearer $ALICE"            # live event stream
```
### Tamper with it

The audit table refuses edits even from its owner (a trigger), and the API's database role has no
UPDATE or DELETE on it at all. To show detection, do what an insider with owner access would, in
the compose database:

```bash
set -a; . ./.env; set +a
docker compose exec -T postgres psql -U $GAWE_DB_USER -d $GAWE_DB_NAME <<'SQL'
UPDATE audit_events SET actor = 'mallory' WHERE run_id = '<RUN>' AND seq = 3;   -- refused
ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete;
UPDATE audit_events SET actor = 'mallory' WHERE run_id = '<RUN>' AND seq = 3;   -- succeeds
ALTER TABLE audit_events ENABLE TRIGGER audit_events_no_update_delete;
SQL
```

Reload the run's audit page and click **Verify hash chain**: it reports `Chain BROKEN at event 3`.
Say what this shows: a casual edit is blocked, a deliberate one by the owner is detected, and only
an owner who recomputes every later hash could hide it, which an external copy of the chain head
would catch. The API's own role (`make app-role`) cannot do any of this.

## 5b. A cancelled run leaves nothing to approve

Start C-1001 as Alice, and before Bob decides, cancel the run (run page, **Cancel run**). Reload
Bob's inbox: the approval is gone, and the audit trail shows `approval.closed`. The same call from
Bob's side after the cancel is refused with 409.

## 5c. Restart safety

Start C-1001, stop `make api` while the run is paused, start it again. The run is picked up again
and still waits for approval; nothing is sent twice.

## 6. The second workflow

Stop the server, set `GAWE_APP=gawe.apps.it_access:bundle`, `make api`, and use profile
`it_access` with the tickets in `src/gawe/apps/it_access/samples/`. Read access to the wiki
completes; admin on the production database waits for a `security_admin` (bob).

## Failure injection

The mock systems can be made to time out: set `faults.fail_next(2)` on a connector in a Python
shell against the same ledger, or run the unit test
`test_transient_outage_is_retried_without_a_second_email`, to see retries produce one effect.
