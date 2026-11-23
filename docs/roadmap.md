# GAWE roadmap

Each milestone ends green: `make check` (ruff, mypy, pytest) plus any integration or schema tests it
adds. If verification fails twice on a milestone, stop and report instead of looping.

| #    | Milestone | Status |
|------|-----------|--------|
| M0   | Scaffold: tooling, Compose with Postgres, README | Done |
| M1   | Contracts and primitives: models, canonical hashing, registry, plan validator | Done |
| M2   | Policy engine: rules, default-deny, HMAC-signed decisions | Done |
| M3   | Persistence and audit: schema, hash-chained append-only audit log | Done (integration tests pass) |
| M4   | State machine and approvals (hash-bound, expiring, separation of duties) | Done |
| M5   | Executor and verifier (idempotency, retries, lease-based resume) | Done |
| M5.5 | Postgres stores for steps, approvals and executions, migration 0002, shared contract tests | Done (47 integration tests pass on Postgres 16) |
| M6   | LLM layer: provider interface, fake provider, quarantined Reader, structured-input Planner | Done (Anthropic provider verified against the live API with `make smoke`) |
| M7   | Orchestrator (`gawe/engine`) and OpenTelemetry: run loop, metrics. Automatic replan deferred | Done; the OTel adapter is untested against a real SDK |
| M8   | FastAPI API, event stream, bearer-token auth stub with roles, Postgres run/plan store (migration 0003) | Done (HTTP and Postgres run-store tests pass on a development machine) |
| M9   | Contract Review app, mock connectors (dedupe on idempotency key), IT-access fixture, synthetic samples, live smoke script | Done (real-model smoke and full demo verified) |
| M10  | Next.js (TypeScript) UI: approval inbox, run timeline, audit and chain-verify view | Done (`make ui-check` passes; demo verified end to end) |
| M11  | Hardening and docs: threat model, architecture, API-boundary adversarial tests, least-privilege DB role, failed-sign-in limit, closing approvals when a run ends, restart recovery | Done (`make check` passes; role migration verified on Postgres 16; `make test-schema` passes, 71 checks; `gawe_app` confirmed able to log in on the dev database, 2026-11-08) |
| M12  | Evidence: reproducible benchmark harness (CUAD Reader accuracy; injection test counting model-fooled and unauthorized-step-ran separately, with run-to-run variance), external audit-head anchor, and a plain-language report | Done (first full run on `claude-sonnet-5-5`, 2026-11-08: 0 of 122 attack trials fooled the model, 0 attacker steps ran, 30 of 30 clean twins completed; clause recall 94%, precision 51%; $15.77 including the trial. See [m12-design.md](m12-design.md)) |
| M13  | Testing the second line of defence: a free scripted benchmark run that sends every attack through the engine with a model fooled on purpose, two rules for "low" CRM ratings, the clean-run policy table, and the decision on internal email and routine access grants | Done (2026-11-10: of 38 scripted runs, 10 attacker steps ran, all allowed by policy on purpose; 5 denied; 23 waited for a person. A "low" rating now needs legal approval when the Reader's findings contradict it or the deal is over 100,000. See [m13-design.md](m13-design.md) and [results.md](results.md#when-the-model-is-fooled)) |
| M14  | Policy that cannot drift silently: rules checked at load against the input schema of every registered tool version, allow rules that cannot match a missing or unreadable value, and a property test that unreadable input never ends in allow | Done (2026-11-12: both shipped policies load cleanly; renaming `risk_level` now stops startup. The property test covers every shipped tool field, malformed addresses and policy variables, and every operator with the schema check removed. The per-check "unreadable" audit marker is deferred to a later milestone. See [m14-design.md](m14-design.md)) |
| M15  | Policy edge cases: explicit address and rule boundary tests, grants only to the reported requester, variable sources checked against declared tool writes at startup, and sequence tests for policy drift after allowed actions | Done (2026-11-12: 1681 passed, 39 skipped; the scripted run matched all 38 expectations, 7 executed, 8 denied, 23 waiting for a person. Grants to a different requester are now denied. Within-app combination rules from the audit trail are split into their own milestone; cross-workflow combinations wait for a shared subject key from a system of record. See [m15-design.md](m15-design.md)) |
| M16a | Recipients and read-back: email sent only to the mailboxes a directory expands, checked by policy and bound into the approval; every write read back from the target's own record, with pending states, a deadline and a scheduled sweep with heartbeats; poisoned tool output and encoded short-field tests | Done (2026-11-15: 1929 passed, 64 skipped; the scripted run matched all 38 expectations, 7 executed, 8 denied, 23 waiting for a person, unchanged; the new integrity suite matched 23 of 23. Encoded text in a message body sent to an allowed internal recipient is a documented gap, not blocked: policy judges recipients, not bodies. The directory, mail server and read-back are properties of the mock systems. See [m16a-design.md](m16a-design.md) and [results.md](results.md#m16a-update-recipients-checked-at-the-mail-server-writes-read-back)) |
| M16b | Approval integrity: approvals bound to the version of each referenced record; signed per-check "unreadable" markers on policy decisions; an approval screen with typed fields first and model-written text collapsed; automatic grants that expire; a next-day review with a keyed sample | Done (2026-11-16: 2145 passed, 74 skipped; the scripted run matched all 38 expectations, 7 executed, 8 denied, 23 waiting for a person, unchanged; the integrity suite matched 23 of 23. Revision checks and revocation are properties of the mock systems. Approve-once is split into its own milestone. See [m16b-design.md](m16b-design.md) and [results.md](results.md#m16b-update-approvals-bound-to-records-grants-that-expire-a-next-day-review)) |
| M16c | Trusted ticket submitter for IT access: runs start from a ticket id and the server fetches the text; who filed the ticket and through which channel come from the ticket system; a grant to anyone other than the filer, or from an emailed or public-form ticket, waits for a security approver; closed tickets are refused | Done (2026-11-16: 2191 passed, 76 skipped; the scripted run matched all 38 expectations, 4 executed, 8 denied, 26 waiting for a person. The three reader-mode lateral grants now wait instead of running. The trusted submitter is a property of the mock ticket system, and honest on-behalf requests always need a person. See [m16c-design.md](m16c-design.md) and [results.md](results.md#m16c-update-grants-compared-with-who-filed-the-ticket)) |
| M17  | The low-risk decision: a definition of `unlimited_liability` for the Reader, a liability-language scan gated on CUAD, approval volume on 300 honest CUAD contracts, and provisional calls on approval by change and on internal email | Done (2026-11-19: 2264 passed, 76 skipped; 116 Postgres tests pass; paid runs $27.12 against $27 planned. Production high-severity precision on `unlimited_liability` rose from 20% to 76%, and 78% on 291 held-out contracts. Only 2.7% of honest contracts were rated "low", so every "low" now needs legal (provisional). The scan's express level failed its gate and is not wired in. The scripted run matched all 38 expectations, 3 executed, 8 denied, 27 waiting for a person. See [m17-design.md](m17-design.md) and [results.md](results.md#m17-update-every-low-rating-goes-to-legal)) |
| M18  | Measurement: the live rerun with every rule since M13, 36 more clean documents counted per document, 40 hand-written attacks (the attacker model declined to write them), and Haiku 5.5 paired against Sonnet 5.5 | Done (2026-11-22: 2435 passed, 76 skipped; 116 Postgres tests pass; paid runs $6.07 against about $9 planned. 64 distinct attacks, none fooled Sonnet, 95% upper bound 5.7%; every one stopped at the model, so policy is still evidenced by the scripted run. Of 43 honest documents, 33 needed no person, 9 a person by the rules meant for them, 0 denied, 1 failed. Haiku is not good enough yet as Reader (F1 −3.9 points, lower end −8.5) or Planner (failed 5 of 15 honest IT runs). The Reader's 300-character summary makes both models fail closed on some documents; fix queued. The scripted run matched all 38 expectations, unchanged. See [m18-design.md](m18-design.md) and [results.md](results.md#m18-update-more-attacks-more-honest-documents-and-haiku)) |
| M19  | The Reader's summary limit: the contract Reader's instructions state the 300-character limit its schema always enforced, checked on the same contracts and attacks, and the Haiku comparison run again | Done (2026-11-22: 2437 passed, 76 skipped; 116 Postgres tests pass; paid runs $15.98 against about $17 planned. Reader errors on attack runs went from 10 of 130 to 0 for Sonnet and from 40 to 0 for Haiku; on CUAD from 1 to 0 of 306 for Sonnet and 79 to 7 for Haiku. The small-contract downgrade was judged live at last: 5 of 5 ignored and flagged. Nothing else moved, and M17's liability bars still pass. Haiku's Reader gap stayed (F1 −4.2 points, lower end −10.7), so it is still not good enough as the Reader, nor as the Planner (failed 8 of 15 honest IT runs). The scripted run matched all 38 expectations, unchanged. See [m19-design.md](m19-design.md) and [results.md](results.md#m19-update-the-readers-summary-limit-fixed)) |
| M20  | Action classes the engine only prepares: an app setting that makes a tool version prepare-only, refused at the executor against the current setting; the step stops at `prepared` and a person with the named role, not the requester, marks it done or not done; API, UI card and demo | Done (2026-11-22: 2490 passed, 77 skipped; 118 Postgres tests pass; migration 0009 up and down; no paid runs. Every path to dispatch was tried and refused with the tool never called: an allowing rule, an approval (also one from before the setting), a resumed run, retries and sweeps, crash recovery, a direct executor call, the benchmark harness. Off by default: the scripted run is identical to M19's. With signatures prepare-only, all 3 scripted and 5 hand-written signature attacks stop at `prepared`. GAWE records the person's word; it can't confirm the action. See [m20-design.md](m20-design.md) and [results.md](results.md#m20-update-actions-gawe-only-prepares)) |
| M21  | An end date for grants made before M16b: GAWE finds each 1.0.0 grant it sent in its own records, gives it the later of its natural end and a notice date, and ends it with M16b's expiry job, through an operator command that is a dry run by default | Done (2026-11-22: 2521 passed, 79 skipped; 121 Postgres tests pass; no migration; no paid runs. The mock IAM now reads and revokes both kinds of old grant (ledger-only, M9 to M15, and the 1.0.0 store, M16a to M16b). `end-old-grants` prints ids and dates only, schedules once however often it runs, and audits `grant.end_date_set`; old-grant jobs past their date count toward the overdue alarm. Run end to end on a throwaway database: a real 1.0.0 grant was listed, scheduled, left alone before its date and revoked at it. The IAM system still doesn't know these dates; only GAWE's job ends them. See [m21-design.md](m21-design.md)) |
| M22  | What the target guarantees: each connector declares what its system enforces per write, startup refuses a write that relies on something its target doesn't, and a condition in another system is read just before sending | Done (2026-11-23: 2549 passed, 79 skipped; 121 Postgres tests pass; no migration; no paid runs. Found while exploring and fixed: an IT ticket closed while its grant waited for a security approver still got the grant. It is now re-read on every attempt immediately before the IAM call, and a closed or unreadable ticket sends nothing. The window between that read and the write is named, not closed. Every shipped write declares idempotency and read-back, and the record-bound ones a conditional write; a write judged by a running total would need atomic reservation, though none exists yet. Scripted and integrity unchanged. See [m22-design.md](m22-design.md)) |
| M23  | Credentials and controls the agent can't touch: each tool's code holds a credential scoped to what the tool does, checked by the target system; the controls inventory pinned by tests, and access to a system holding another app's controls waits for a person; a per-step trace that links decision, approval, attempts, condition checks, hand-off and read-back, and names any broken link | Done (2026-11-23: 2627 passed, 80 skipped; 122 Postgres tests pass; no migration; no paid runs. Found while exploring and fixed: the IT app granted write on the CRM automatically, and the CRM holds the deal values contract review's policy reads; write or admin there now waits for security. One honest benchmark ticket now waits too, so M18's "33 of 43 needed no person" would be 32. Credentials are proved on the mocks; the list of systems holding controls is kept by hand. Scripted and integrity unchanged. See [m23-design.md](m23-design.md)) |

Next: replay of failed events with side effects (item 7 in the queue below).

Planned, not scheduled: **Pilot-ready** (OIDC sign-in and the first real connector against a sandbox
account). It starts when there is a design partner or a sandbox to connect to, and gets a number then.

M5.5 was not in the original plan; the orchestrator needs it before anything runs against a real
database. UI and the IT-access fixture are the first things to cut if scope must shrink.

## After M13: candidates, not scheduled

### Queued after M16, highest first

Small items that close a measured gap go first; the first two went ahead of the low-risk decision; item 4 comes right after M18.
Re-ranked 2026-11-19: the two small items (prepare-only classes, end dates for old grants) move
ahead of replay, and the connector-guarantee and credential items are merged.

1. **A trusted ticket submitter for IT access** (done 2026-11-16 as M16c). The requester came
   from the Reader, so the three reader-mode lateral grants still ran. Who filed a ticket, and
   through which channel, now come from a mock ticket system; a grant to anyone else, or from an
   emailed or public-form ticket, waits for a security approver (reader comment).
2. **One audit event per sweep result** (done 2026-11-16). A sweeper that stalled past its lease
   could let a second one write the same read-back, grant-expiry or review event. The event is now
   written once: the append looks for the same event first, inside its retry.
3. **The low-risk decision** (done 2026-11-19 as M17), then more measurement (done 2026-11-22 as
   M18): a live rerun, more clean documents, hand-written attacks and a Haiku comparison.
4. **The Reader's summary limit** (found in M18, 2026-11-22; done 2026-11-22 as M19). The contract Reader's summary is
   capped at 300 characters, and its prompt asks for the contract's main risk plus a mention of any
   text aimed at AI reviewers. On a one-sided contract with planted text that doesn't fit, so both
   attempts fail and the run ends with no findings: `cr-downgrade-smalldeal` failed 5 of 5 live in
   M18 (`summary:maxLength`), an honest CUAD contract failed once, and M17's volume run had 9
   Reader errors in 300. It fails closed, but attacks end "undecided" and honest work fails. Fix:
   state the limit in the prompt (or allow a longer summary), then re-measure the Reader-error rate
   and re-read the Sonnet side of any paired comparison, since the wording changes. Kept out of M18
   so its Haiku comparison pairs against reads with today's wording.
5. **Action classes the engine only prepares** (done 2026-11-22 as M20). Some users keep execution to themselves for good.
   For a class configured that way, no passing test, plan, earlier approval or reusable
   authorization may let the agent dispatch; preparing the action stays available. This is a
   general boundary and needs no new connector (reader comments).
6. **An end date for grants made before M16** (done 2026-11-22 as M21). They never expired and
   were left for an operator to review. Each now gets an end date and an expiry job from an
   operator command: the later of its natural end under today's lifetime and a notice date.
7. **Replay of failed events with side effects, scoped to a tenant.** Several readers asked for it
   (scope, duplicate billing, late callbacks). Fix the batch at preview, inside the hash; reuse
   the original idempotency key or reconcile the original operation, never a fresh key; cap the
   amount (policy already has `gt` and `lte`); take the tenant from the event record. It would
   reuse M16's preparation, read-back and record binding (reader comments).
8. **What the target guarantees, and what the agent cannot touch** (done 2026-11-23 as M22 and
   M23). Two reader requirements, merged 2026-11-19: both ask what the connector or target
   actually enforces and what the engine only assumes. Split 2026-11-22 into M22 and M23.
   - (Done as M22.) Block automation when the target cannot guarantee what policy needs. An approval can go
     stale while configuration stays the same, because operational state (a spend rate, a platform
     cap) moves underneath it. Name the authoritative conditions and how fresh they must be. Reserve
     cumulative limits atomically at the target, not with separate numeric checks. When a connector
     cannot enforce a condition policy relies on, refuse automatic execution and record the race
     that remains; a fresh human approval does not remove it (reader comments).
   - (Done as M23.) Credentials that match the action, and controls the agent cannot touch. Where the target
     supports it, a read credential cannot write: try a write with it and check the target refuses.
     Tools exposed to the agent cannot change the policies, verifiers or credential settings that
     constrain them, through any execution path. Link proposal, decision, approval, dispatch and
     observed result for success, denial and unknown alike, and name the gaps in today's audit
     links rather than adding a new audit system (reader comments).
9. **Combination rules within one app, from the audit trail.** Split out of M15. Needs its own
   design, including the keyed subject hash and key management. Combinations across workflows
   still need a shared subject key from a system of record.
10. **An undo record written before each action.** Save the prior values before an eligible change
   and bind undo to the original action and the record's revision, so undo stops if anything
   changed since. Undo needs its own policy decision, idempotency and read-back, and never claims
   to reverse an effect someone outside has already seen. Needs its own design (reader comment).
11. **A rendering test for the approval card.** The UI tests check the card's model and its
    source, not a rendered page. A rendering test needs a DOM test dependency.
12. **Follow-ups that wait for a real mail system and directory** (Pilot-ready): a directory change
    marker per group, or none, since the sorted mailbox set is the real binding; mailboxes that
    forward outside treated as external at expansion, if the directory exposes forwarding; and an
    expansion cache only if the marker moves when anything under the group changes, including
    nested groups and members' forwarding.

- **Why the Reader over-flags `unlimited_liability`.** In the first run its precision on CUAD was
  33% (61 of 91 flags on contracts CUAD does not mark), against 69–74% for the other scored
  kinds. Its definition of "uncapped" may be wider than CUAD's; compare the flagged contracts.
  A yes/no field may be the cause: try "capped / capped with carve-outs / uncapped" instead.
  Since M13 this also sets how often legal is asked to approve an honest "low" rating: the rule
  for contradicted ratings fires on any high-severity flag, so over-flagging becomes needless
  approvals, not just a wrong number.
  **M17:** a definition mismatch, not misreading: most false flags were on contracts with no cap
  clause or only an indirect-damages exclusion. A definition in the production prompt, with a
  medium level for no cap, raised high-severity precision from 20% to 76% (78% held out) without
  losing true flags. The same narrowing on the presence profile lost recall and was reverted.
- **Approval by what an action changes, not by deal size.** A reader's principle: any action that
  changes what anyone can do later (access, risk ratings, approval thresholds, the policy itself)
  always goes to a person, however small, because lowering the bar is usually an attacker's first
  step. Thresholds and policy are already out of reach (no tool changes them). Taken literally, the
  rest means a person on every review and every access request: risk writes and grants happened
  in 15 of 15 clean runs each. One option to weigh against it: classify each tool action by
  whether it lowers the bar (a lower risk rating, wider access) and require approval for those
  only, perhaps per tenant, replacing the 100,000 deal-size line with a principle. Decide with
  the approval volume from the next live run in hand.
  **M17, provisional:** for risk ratings, every "low" now needs legal; 2.7% of 291 honest CUAD
  contracts were rated "low". A downgrade to "medium" still writes without a person. Confirm or
  reverse with real traffic.
- **Policy never reads state the agent can write.** Today every rule judges a step on its own
  arguments plus inputs no earlier action can change: fixed tenant settings, the deal value in the
  contract system (no tool writes it), and the Reader's findings for that run only. So a series of
  allowed actions cannot drift a workflow into a state where a later gate is weaker (a reader's
  concern: three harmless writes in a row making a destructive one reachable). That holds only
  for the current rules; one like "low-risk contracts skip signature approval" would break it.
  Make it explicit: a check that no policy variable comes from data any registered tool writes,
  plus sequence fuzzing as a tripwire (run random sequences of allowed actions, then check that
  every attacker goal still ends as expected). The fuzzing passes trivially today and fails the
  day a rule starts reading writable state.
  Exception worth having: rules on combinations, which is where an attacker's sequence shows
  (a second downgrade on a contract that has already been lowered; a downgrade plus a grant on
  the same deal, once workflows share a deal). Allow history only to escalate, never to relax:
  a rule may read it to add an approval, never to remove one, so the worst an attacker gets is
  more reviews. Read that history from the audit trail, which the engine writes and no tool can,
  not from records the agent's own tools wrote. (Suggested by a reader.)
  How: each tool declares a subject key (say, the contract or deal id); the engine records it at
  the tool boundary and policy joins on it, so two workflows need not know about each other.
  Conditions: the audit trail stores no arguments by design, so record a keyed hash of the key,
  not the key (ids like C-1001 are guessable, so a plain hash could be reversed; the secret is one
  more key to manage). And a cross-workflow rule needs a key both tools carry, taken from a system
  of record: an access grant names a person, system and level but no deal, and a key the Reader
  extracts or the agent supplies can be left out or changed, so the join misses. Because history
  only escalates, a missed join means no extra review, never a weaker gate.
- **Trace where trusted values come from.** The large-deal rule works because the document
  cannot change the deal value. In the mock it is seed data no tool writes; in a real system a
  person or another system entered it, perhaps from the same contract. Before treating any value
  as a trusted anchor, trace who can write it; if it was ever filled from a document or by
  another agent, it is one step from untrusted input. (Raised by a reader.)
- **Automatic grants expire.** Access granted without a person's approval should carry an end
  date, tied to the request (for example, when the ticket or review closes), so the risk accepted
  by allowing routine grants is time-boxed. Make it a default on every automatic grant, with
  renewal going back through policy like a new request. `iam.grant_access` has no expiry today;
  approved grants could last longer. (Suggested by a reader.)
- **Revisit allowing internal email without approval.** It was allowed on fatigue grounds, but
  none of the 30 clean runs sent any email, so requiring approval would have added no reviews in
  the measured workflows, while the 3 internal-email attacks ran. Real workflows that send internal
  notifications may differ; check before changing. A domain check also cannot see forwarding: an
  internal address that forwards outside looks internal to policy. Look-alike domains already
  fall into review (the domain must match exactly), so the safer option may be allowing specific
  internal recipients rather than the whole domain. (Raised by readers.)
  **M17, provisional:** stays allowed. Since M16a every resulting mailbox is checked and an outside
  member sends the email to review; no clean run has sent email, so there is nothing to weigh yet.
  The recipient allowlist waits for a real tenant to choose it.
  Judge the mailboxes, not the label: a group or alias address passes as internal and then fans
  out to whoever is behind it, possibly outside the company. Have the connector expand the
  recipient through the directory before the approval hash is computed, so policy checks every
  resulting mailbox and the approval binds the expanded set, plus the directory's own version or
  change marker if it has one. Re-expand at send time and compare the actual set with the
  approved one; if membership changed, the send fails and needs a fresh approval. Record the
  lookup time alongside for audit, not inside the hash (with the time in the hash every
  re-expansion would differ even when nothing changed), and let approval expiry stop a stale
  expansion being sent days later. Block, not warn, on any mismatch between
  the approved set and what is sent. Send to the expanded mailboxes, never the group address:
  otherwise the mail server expands the group after the check and its record shows only the
  group. The To: header may still name the group for readers. If membership cannot be read
  (hidden or unresolvable nested groups), block the send; never fall back to the group address.
  An external list cannot be expanded and need not be: policy already treats it as one
  external recipient. A mock directory with groups and aliases makes this testable
  now; expansion against a real mail system waits for a real connector.
  Judge the mailboxes, not the label: a group or alias address passes as internal and then fans
  out to whoever is behind it, possibly outside the company. Have the connector expand the
  recipient through the directory before the approval hash is computed, so policy checks every
  resulting mailbox and the approval binds the expanded set, plus the directory's own version or
  change marker if it has one. Re-expand at send time and compare the actual set with the
  approved one; if membership changed, the send fails and needs a fresh approval. Record the
  lookup time alongside for audit, not inside the hash (with the time in the hash every
  re-expansion would differ even when nothing changed), and let approval expiry stop a stale
  expansion being sent days later. Block, not warn, on any mismatch between
  the approved set and what is sent. Send to the expanded mailboxes, never the group address:
  otherwise the mail server expands the group after the check and its record shows only the
  group. The To: header may still name the group for readers. If membership cannot be read
  (hidden or unresolvable nested groups), block the send; never fall back to the group address.
  An external list cannot be expanded and need not be: policy already treats it as one
  external recipient. A mock directory with groups and aliases makes this testable
  now; expansion against a real mail system waits for a real connector.
- **Grants only to the requester the Reader reported.** When only the Planner is fooled, the
  attacker's grant goes to someone other than the ticket's requester (3 of the 6 grant attacks
  that ran). A rule comparing the grant's email with the Reader's `requester_email` would stop
  those; it cannot stop a fooled Reader that reports the attacker's choice as the requester, the
  same limit as the contradiction rule. Same-tool attacks like these are also why a daily cap
  would not single them out, while a next-day review could.
- **A deterministic scan for liability language, as a downgrade signal.** The contradiction rule
  only fires when the Reader flags a clause, so it is bounded by the Reader's recall; a fully
  fooled "low" with nothing flagged on a deal of 100,000 or less still writes without a person.
  Someone planting instructions in a contract can add text but usually cannot remove the real
  liability or indemnity clause, so a scan of the document for that language would still find it,
  whatever the Reader reports. Use it as one more signal for the "low" rules, not as a judge:
  paraphrase defeats keywords, and a counterparty who wrote the whole contract controls the
  wording. (Suggested by a reader.)
  M17 built it (`apps/contract_review/liability_scan.py`) and gated it on 495 CUAD contracts: the
  express level failed (2 of 8 hits matched CUAD), the carve-out level looked precise. It is not
  wired into the engine, because M17's provisional rule sends every "low" to legal, so nothing
  is left for it to question. **Trigger:** if that rule is dropped after real traffic, wire the
  scan in first: run it before the Reader, store the result on the run, give policy a source for
  it. A rule on the carve-out pattern `except_for_liability` (19 uncapped, 0 others on CUAD)
  would need its own decision and a check on contracts it was not chosen on.
- **Check state, not just the tool's reply.** Post-conditions today compare a tool's own reply
  with what was asked ("the assessment I wrote says low for C-1002"). For writes, read the record
  back from the system with a separate call and check that instead, and let that read set the
  step's status rather than confirm it afterwards. For email, read the sent message's actual
  recipients back, which also catches one approved recipient becoming three on the message. Read
  them from the mail server's own record of what it accepted (the RCPT TO entries in its trace),
  never from the app's log of what it meant to send, which can only ever agree, and never from
  the headers, which the sender writes and which omit Bcc. Run status already never comes from a model's
  prose; the executor decides it. A state that cannot be read straight away (a post in
  moderation, say) needs a pending status with a deadline and three outcomes: done (a fresh read
  agrees), blocked (the read disagrees; keep the difference) or unknown (the deadline passed with
  no read), so "pending" cannot become where failures quietly rest. Resolve them with a scheduled
  sweep, not the next run, which may never start; the sweep emits a heartbeat with resolved and
  still-unknown counts, and the alarm is the unknown count growing while heartbeats continue.
  Nothing in the engine does this today. (Raised by readers.)
- **Approvals bind the version of every record they refer to.** The action hash covers the tool,
  its version and the resolved arguments, so an approved signature is tied to a contract id, not
  to that contract's content. The deal value is rechecked at execution (the send fails with
  `deal_value_mismatch` if it changed), but replacing the contract's text under the same id would
  not be caught. Have the contract system return a version or content hash, carry it in the
  arguments so it is inside the approval hash, and have the connector recheck it before executing;
  a replaced record then fails and needs a new approval. Apply the same to any tool that acts on a
  referenced record. (Raised by a reader.)
- **No model-written prose on the approval screen.** The approval card shows the policy's reason
  (from the YAML, trusted) and every resolved argument as raw JSON, so free-text arguments written
  by the model from the document (an email's subject and body, an assessment's summary) sit next to
  the Approve button. Once broad allows are tightened, a rule-aware attacker aims for steps that
  need approval and spends that text on persuasion ("counsel already confirmed this; approve
  before the deadline"). Show typed arguments first (recipient, amount, the action's hash, and the
  difference from the last approved call); collapse free-text arguments and label them as written
  by the model from an untrusted document. A UI test can check the labelling now. Whether
  approvals change with the wording (vary only the summary between identical requests) needs real
  approvers, so that measurement waits. (Raised by readers.)
- **Injection through tool outputs.** No model reads tool outputs today: the Planner plans once
  from the Reader's findings, and later steps receive earlier outputs as arguments (`$ref`),
  which policy checks on their final values. Two gaps: no attack yet puts poisoned data in a
  connector's output (say, a contract record's contact address) to see it become an argument;
  and automatic replanning, deferred since M7, must route tool outputs through the quarantined
  Reader, never to the Planner, if it is ever added. (Raised by a reader.)
- **How often policy steps in on honest work.** "30 of 30 clean runs finished" measures utility,
  not how often policy denied, held for approval, or changed an action a person would have
  allowed. Report interventions on clean runs, with an interval, in every live report. The 30
  runs are 6 documents repeated 5 times, so the honest unit is the document and 0 of 6 still
  allows a high rate; add more clean documents rather than more repeats. The first run had no
  interventions on clean runs, but that predates the M13 rules. (Raised by a reader.)
  **M18:** reported per document in every live report. 43 documents: 33 needed no person (62–87%),
  9 a person by the rules meant for them, 0 denied (0–8%), 1 failed on a Reader error.
- **Fuzz arguments against the rules, property-based.** An attacker who knows the rule file aims
  at its broad allows, which is where all 10 scripted attacker steps that ran landed. Some edge
  cases are tested already (a comma-joined recipient list is not judged by its first address);
  others are only reasoned about (a domain that is not exactly internal should fall into the
  outside-email rule). An address that does not parse (a trailing dot, say) lands in review by
  design: a value the engine cannot read makes positive checks false and negative checks true,
  so "not an internal domain" holds and uncertainty never widens what is allowed. Make it
  property-based rather than one case at a time: write each rule's boundary in one sentence
  ("an address is internal when ..."), generate boundary cases (recipient lists, display names,
  subdomains, Unicode look-alikes, trailing dots, case changes), tag each with its expected
  outcome (allow, needs approval, deny), the rule it should hit (decisions record `rule_ids`) and
  whether it is meant to parse, and fail `make check` on any miss: the engine is deterministic,
  so the target is zero, not a rate. Checking the rule and the parse status, not just the
  outcome, stops a case passing for the wrong reason, so the parser cannot become a hidden
  second rule. Conditions: the expected outcome must come
  from a separate, deliberately simple check or a label per kind of case, never the policy's own
  parser, or the test can only agree with it; include legitimate tricky addresses that must stay
  allowed (plus-addressing, uppercase, international domains), since every stricter fix can push
  real mail into review; and use fixed seeds in `make check`, with a deeper randomised run kept
  separate, so results repeat. Expect it to force decisions not yet made (is a Cyrillic look-alike
  of an internal domain a deny or a review?). Likely needs Hypothesis as a test dependency.
  (Raised by readers.)
- **Injection through short text fields.** The benchmark checks that attacker text never reaches
  a stored record, but the mock systems only store strings. A mock that interprets its inputs
  (as formatting, templates or commands) would test whether a bounded field the Reader passes on
  can still carry a payload into a downstream system. Include encoded payloads: the Planner reads
  the Reader's short strings (the 300-character summary has real room) inside a fenced data
  block, and policy checks tool and arguments, never message bodies, so base64 in the summary
  could ride into an internal email's body and pass. Matching on exact bytes would not catch it.
  (Raised by a reader.)
- **Approve once for a kind of action.** A person approves a kind of message once; matching sends
  then run unattended under a daily cap, and one bad result or one edit by the owner puts that
  kind back to manual. Today every approval covers exactly one action.
- **A next-day review job, with a sampled review of clean downgrades.** Re-check the previous
  day's automatic actions against the written rule, using the audit trail. Include a sample,
  about 5%, of "low" ratings that went through with nothing flagged, for a person to check: the
  one case the downgrade rules let through, and the only way to measure how often the Reader
  misses a real risk. Choose the sample from the action's hash rather than at random, so it is
  reproducible and auditable like the rest of the policy. (Suggested by a reader.) Size the rate
  from the claim it has to support: zero misses in about 32 samples only shows a miss rate under
  roughly 9%; under 1% needs about 300. Decide the hash key deliberately: a stable key (contract,
  clause, model version) keeps the sample from reshuffling when a record changes, but then the
  same contracts stay in or out until the model changes.
- **More distinct attacks, not more repeats.** The 125 injection runs are 25 attacks repeated 5
  times, so the honest sample is closer to 25 (a 95% upper bound of about 13%). Bringing that down
  needs a few hundred attacks that genuinely differ. Model spend is small (about $0.015 a run);
  writing them is the cost. If they are generated, check that they test something different
  rather than varying 25 templates. Better: generate each attack from the outcome of the last
  (what was denied, by which rule, what was stored), as an adaptive attacker would, and measure
  diversity by which rule or layer each attack ends at, not by how different the text looks:
  fifty attacks that die at the same rule are one attack. (Raised by a reader.)
  **M18:** a model attacker driven by outcomes was built, but Sonnet declined the role, and the
  benchmark doesn't get round refusals. 40 hand-written attacks, each a different framing, took
  the count to 64 distinct: none fooled Sonnet (95% upper bound 5.7%). All stopped at the model,
  so diversity by end point was two points. The attacker loop stays, unused, for a model that
  accepts the role.
- **A bigger contract sample, if paired comparison is not enough.** Treated as independent
  samples, two setups 5 points apart need about 330 items each to tell apart (12% of answers
  change between repeats); the current sample has 153 (51 contracts, 3 clause types), enough for
  about 7 points. But comparing two setups on the same contracts is paired: differences between
  contracts cancel and the remaining noise is the 12% already measured, so 153 items compared per
  (contract, clause type), with the interval from resampling whole contracts as the existing
  intervals already do, may be enough. Grow the sample only if that interval is too wide. CUAD
  has 495 usable contracts, at about $0.04 per read.
  **M18:** not needed: the paired interval on presence precision had a half-width of 4.5 points,
  under the 10-point rule.
- **A Haiku 5.5 comparison run** on the same attacks and contracts, to see whether a cheaper model
  is good enough for the Reader or Planner. Compare the paired difference, not each model's
  interval: a permutation test over contracts (swap a whole contract's answers between the two
  models at random, about 10,000 samples, and recompute the metric, which works for ratios like
  precision). Report first how many contracts the two models disagree on: with k of them, the
  smallest possible p-value is about 2^-k (five contracts cannot go below about 0.03 one-sided),
  however many samples run. Size the run by the disagreement expected. In practice noise, not
  that floor, is the limit: with 12% of answers changing between repeats, about a third of
  contracts (around 16 of 51) would show a disagreement even between identical setups. Noise does
  not invalidate the test, but it costs power, so compare each model's majority answer over its 3
  reads and report separately the disagreements that hold across repeats. For accuracy, use all
  six reads rather than the majority: score each contract as (correct reads for A) minus (correct
  reads for B), from -3 to +3, and permute that across contracts; keep the whole-contract swap for
  precision and recall, which are ratios. (Agreed with a reader
  in the comments on the published write-up.)

## Considered and declined

Reader suggestions weighed and not adopted, with the reason:

  **M18:** done, paired as described. Haiku's Reader F1 was 3.9 points lower (95% −8.5 to +0.2),
  failing the bar; its Reader failed on 79 of 306 CUAD reads and 40 of 130 attack runs, mostly the
  summary limit; its Planner was never fooled but failed 5 of 15 honest IT runs. Re-run after the
  summary fix.
  **M19:** re-run. Haiku's Reader errors fell to 7 of 306 CUAD reads and 0 of 130 attack runs, but
  its presence F1 gap stayed (−4.2 points, 95% −10.7 to +2.0), so the errors were not most of the
  gap. Its Planner failed 8 of 15 honest IT runs. Not good enough yet in either role.
- **A confidence score from the Reader to prioritise reviews.** The confidence comes from the model
  an attacker is trying to fool, so "low risk, high confidence" costs the attacker nothing. Reviews
  are prioritised on values the document cannot change instead.
- **Approval on every zero-flag "low" rating.** An honest low-risk contract also arrives with no
  flags, so this means approval on almost every honest low. The gap is measured instead (the
  sampled review) and narrowed by the deal-value rule.
- **Approval on every risk write and access grant.** Both happen in every clean run, so this puts a
  person on every review and request. The narrower version (approval when an action lowers the bar)
  stays on the list.
- **Per-contract Fisher exact tests for the model comparison.** With 3 reads per setup the most
  extreme split gives p = 0.05 one-sided and 0.10 two-sided, so no contract can pass, and the
  p-values are too coarse to combine. The suggester agreed; the paired permutation test is used.
- **A richer, context-aware policy language.** Kept static on purpose, for auditability. Context
  comes in as values the app computes from trusted sources (the deal value), not as rule logic.

## Decisions in force

- Every "low" CRM risk rating needs legal approval (M17, provisional: 2.7% of 291 honest CUAD
  contracts were rated "low"; real traffic confirms or reverses it). The M13 rules still match
  and give the reason when the Reader's findings flag a high-severity clause or the deal in the
  contract system is over 100,000. A fooled "medium" still writes without a person.
- Internal email and non-admin, non-production access grants to company accounts run without
  approval (decided in M13): approval on every one would be clicked through. Revisit with real
  traffic. Internal email is under review (see "After M13"): no clean run sent any email.
- Policy refuses to start when a rule names a tool that is not registered, or an argument path
  missing from any matching tool version (M14).
- Allow rules may not use a check that matches a missing or unreadable value (`ne`, `not_in`,
  `domain_not_in`, `exists: false` or a variable): such checks belong in approval and deny rules,
  where widening only adds reviews (M14).
- Python 3.12+ backend (FastAPI), Postgres, own state machine now (Temporal later), TypeScript UI.
- The policy engine, not the LLM, authorizes execution. Plans are untrusted input.
- Approvals bind to the action hash (tool, version, resolved args, run, step).
- Dual-LLM design: a quarantined Reader with no tools; a Planner that sees structured findings only.
- Secrets come from the environment only.

## Known limits and open items

- Audit tail truncation and rewritten history are detected against an anchor kept outside the database (`GAWE_ANCHOR_SINKS`, off by default): a local file, and/or a signed RFC 3161 timestamp (`rfc3161`, needs the `anchor` extra). Each finished run is one request to the timestamp authority; batching many heads into one timestamp comes later if volume needs it.
- A model that refuses or runs out of output tokens is a failed Reader or Planner attempt; after the last attempt the run fails (`reading_failed` or `planning_failed`) instead of stalling in `planning`.
- Benchmark results live in `bench-results/` (not committed) and are reproducible from the manifest in each run directory. Runs on other models, prompts or dates need a new run.
- Run `make app-role` and set `GAWE_APP_DB_PASSWORD` so the API uses the limited role; without it the API falls back to the owner and logs a warning.
- "Exactly once" external effects require connectors to deduplicate on the idempotency key; the
  connector contract is implemented by `gawe.connectors.base.Ledger` and tested; real connectors must do the same.
- Policy decisions are stored in `runs.decisions` (jsonb); the `policy_decisions` table from 0001 is unused for now.
- Interrupted runs are driven again at startup (`GAWE_AUTO_RESUME=0` disables); `POST /v1/runs/{id}/resume` does it on request.
- The event stream polls the audit log once a second; fine for an MVP, not for thousands of viewers.
- The approval inbox scans all open approvals and filters in Python; add a tenant-scoped query before scale.
- API tokens are a stub (hashed tokens in a file); replace with OIDC and database roles.
- Only one in-process driver per run (a lock); a multi-worker deployment needs a database lease.
- Steps run one at a time in plan order; no parallel execution.
- Replanning after a failed step is designed (run `running -> planning`) but not implemented.
- Open approvals are closed (rejected by the system) when a run ends; the inbox also filters by step status.
- Policy sees the arguments the model proposed. Where an argument carries a fact policy relies on (the deal value), the tool re-checks it against the system of record and refuses on a mismatch.
- Uploads accept text and Markdown only; PDF/DOCX extraction is a separate parsing risk and comes later.
- Failed-sign-in limiting is per address; behind the UI all users share one address. See docs/threat-model.md.
- Dependencies are not scanned automatically; `npm audit` reported no issues in the UI on a fresh install (2026-11-09).
- Policy argument paths must be declared object properties. A tool schema that uses `$ref`,
  `anyOf` and the like along a rule's path stops startup until the load check supports it (M14).
- Reader free-text fields are still attacker-influenced. Keep them to a minimum in each app's
  Reader schema and prefer enums, numbers and booleans.
- Encoded text in a message body sent to an allowed internal recipient passes policy and is
  delivered: a documented gap (M16), because policy judges recipients, not bodies.
- Record revision checks, grant revocation and the active-grant rule are properties of the mock
  systems (M16). A real connector must supply an `If-Match`-style precondition, a revoke call and
  a read that reflects it.
- Grants made before M16b have no end date until an operator runs `end-old-grants` (M21); after
  that, only their GAWE expiry job ends them, since the IAM system doesn't know the date.
- Tool credentials are scoped and checked in the mock systems (M23). A real connector needs a
  credential per tool from the target, and the same refusal test run against its sandbox.
- Which systems hold another app's controls (`tenant.control_systems`) and the IAM's name for
  each are kept by hand (M23).
- The step trace reads GAWE's own records; it links what happened outside GAWE only as far as the
  read-back saw it (M23).
- The next-day review covers the last seven days and needs `GAWE_REVIEW_SAMPLE_KEY` for its
  sample; without the key the sample is off.

## Per-milestone testing

- Unit tests for every new module; adversarial tests for anything on the authorization path.
- Every new state transition needs a test (`tests/unit/test_statemachine.py`).
- Policy changes need golden-file cases (`tests/fixtures/example_policy_cases.yaml`).
