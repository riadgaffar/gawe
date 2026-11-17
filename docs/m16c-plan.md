# M16c build plan

Status: **Done.** S1–S3 approved and committed by the owner and merged to `main` (2026-11-16). Plan approved by the owner on 2026-11-16. It implements D1–D5 in
[m16c-design.md](m16c-design.md), approved by the owner on 2026-11-16 as recommended.

## Working rules and verification

Work on `ticket-submitter`. Do not commit, stage or push; the owner reviews and commits each
checkpoint. No live model calls and no real connectors. Contract review does not change. Build
nothing outside D1–D5: no real ticket system, no sign-in integration, no early grant end on ticket
closure, no approval-by-change.

Run S1–S3 in order and stop for owner review after each. At each checkpoint give:

- the diff summary;
- the actual verification output, with a complete local log in an ignored
  `bench-results/m16c-sN-*.txt` file;
- known limitations;
- a suggested one-line commit message.

If verification fails twice on the same step, stop and report. A change to approved security
semantics needs a new owner decision; routine details may be settled here and recorded.

At each checkpoint run `make check` and `make check-docs`. For schema changes also run
`make test-schema`, an Alembic upgrade and downgrade on a throwaway database, and
`make test-integration`. For UI changes run `make ui-check`. Run `python -m bench scripted` and
`python -m bench integrity` and compare every case, not just the exit code.

Settled routine detail: the engine's run record carries a generic `source_id`, the id of the
record a run's document came from, and the app resolves it. "Ticket" stays an IT-access word.

## S1. A run bound to its source record (D2)

- Migration 0008 (SQL and Alembic, after 0007): `runs.source_id` (text, nullable). Run stores
  round-trip it.
- `AppBundle.sources`: an optional resolver per profile, `fetch(tenant_id, source_id)`, returning
  the document text or failing closed with a code.
- A profile that declares a source requires `source_id` and refuses an uploaded document. The
  server fetches the body and stores it as the run's document, then clears it when the run ends,
  as today. A profile without one is unchanged.
- API: `POST /v1/runs` accepts `source_id`. The run view shows it. The UI start form gets a
  "Ticket id" field for profiles that declare a source.
- A mock ticket system (`connectors/mock_tickets.py`, its own SQLite file in the state
  directory, lazy like the other mocks). Each ticket holds `{ticket_id, tenant_id, submitted_by,
  channel, status, body}`. `submitted_by` and `channel` are set at creation and cannot change.
  The three sample tickets are seeded, each filed by its requester through `portal`.
- IT access declares the ticket system as its source. Nothing in policy changes yet.
- The benchmark's IT-access cases start runs by ticket from here on. Each case seeds its own
  ticket: the base ticket's requester as submitter, `portal` as channel, and the attacked text as
  the body. With policy unchanged, every scripted record must stay identical: binding alone
  changes nothing.

Tests:
- the source resolves and becomes the document;
- an unknown, foreign-tenant or unreadable ticket fails before planning;
- an upload to a sourced profile is refused;
- the document is cleared at the end of the run;
- `submitted_by` and `channel` cannot be changed;
- contract review starts exactly as before;
- the store round-trip on memory and Postgres;
- the API field and the UI form.

The schema test covers the new column.

Suggested commit: `feat(runs): bind a run to its source record and fetch its document`

## S2. Policy reads the trusted submitter (D1, D3, D4, D5)

- IT-access variables:
  - `request.reported_requester` comes from the Reader (formerly `request.requester_email`);
  - `request.submitters` comes from the ticket's `submitted_by`, as a `resource` source;
  - `request.portal_submitters` is the same list for a `portal` ticket, and empty otherwise.
- Provenance declares all three. Tools must not write any of them, checked at startup as today.
- Policy keeps `requester-mismatch-denied` (D4 as revised: the grantee must be the reported
  requester) and adds:
  - `grant-to-other-than-submitter-needs-security` (D3): `email` not in `request.submitters`;
  - `untrusted-channel-needs-security` (D1): `email` not in `request.portal_submitters`.
- A missing ticket leaves the ticket variables unresolved, so policy denies (`engine.error`), as
  for any unreadable input.
- Preparation for `iam.grant_access@2.0.0` reads the run's ticket and refuses with
  `ticket_not_open` when it is closed or cancelled (D5). A missing or unreadable ticket fails
  closed with its code. Preparers now receive the run as an optional keyword; others ignore it.
- More mock tickets for tests: an `email` channel ticket, a `public_form` ticket, a ticket filed
  on another's behalf, a closed one, and one whose text names a different ticket id.

Tests:
- a grant to the submitter is automatic;
- a grant to anyone else waits for `security_admin`;
- an untrusted channel waits;
- a grant to someone other than the Reader's requester is denied, as in M15;
- a fooled Reader's lateral grant waits for approval, including when the grantee is the
  submitter's direct report (the Lee and Sam case);
- an honest on-behalf ticket waits for approval;
- a closed ticket fails in preparation;
- text naming another ticket changes nothing.

Existing boundary, unreadable-input and sequence tests are updated for the new variables. The
property "unreadable input never allows" still holds.

- The expected records for `it-lateral-comment`, `it-lateral-toolcall` and `it-lateral-split` in
  reader mode change from "ran" to "waiting for approval". This is the measured gap closing; the
  owner approves the change at this checkpoint. Totals move from 7 executed, 8 denied and 23
  waiting to 4, 8 and 26, and every other recorded field stays identical. The integrity suite has no
  IT-access cases and does not change.

Suggested commit: `feat(it-access): compare grants with the ticket's trusted submitter`

## S3. Cumulative verification and completion record

- Review the cumulative diff for:
  - scope;
  - compatibility (runs without a source, migration 0008, contract review untouched);
  - privacy (no ticket text in logs, audit payloads or heartbeats);
  - changed expectations.
- Negative matrix, each with a test that fails when its guard is removed, on an exported copy:
  - the requester taken from the Reader again;
  - a grant to someone other than the submitter allowed;
  - an untrusted channel trusted;
  - the Reader cross-check removed;
  - a closed ticket granted;
  - an uploaded document accepted for a sourced profile.
- Run every check above and both benchmarks.
- Update the threat model (T22 and a new row for an untrusted submitter), the results page, the
  API docs and both completion records.

Suggested commit: `docs: record verified M16c trusted-submitter results`

## Approval and progress record

- Phase 2: design D1–D5 and the name M16c approved by the owner on 2026-11-16, as recommended.
- Phase 3: this plan, approved by the owner on 2026-11-16.
- Routine change at the start of Phase 4: the planned S3 (benchmark through tickets) is folded
  into S1 and S2. Otherwise S1 would break the benchmark's IT cases, and S2's policy change would
  break three expected records until S3. The cumulative step is now S3.
- S1: approved by the owner and committed.
- D4 revised by the owner at the start of S2 (see the design): the Reader's requester is checked
  against the grantee, not the submitter. Expected totals for S2 become 4 executed, 8 denied and
  26 waiting.
- S2: approved by the owner, including the three changed benchmark expectations, and committed.
- S3: cumulative review, negative matrix (6 of 6 guards caught) and final verification;
  approved by the owner and committed, then merged to `main` by fast-forward
  (results in the design's build record). Two failures predate M16c and were
  fixed after M16c: an intermittent executor race test, and a Next.js security advisory that
  stopped the UI build.
