# M12 build plan

Status: **done** (2026-11-08). Every step below was built and verified, and the live gates ran:
L1 (CUAD pinned after its MD5 matched Zenodo's), L2 (free count), L3 (trial, $0.35) and L4 (the
full run, $15.42, resumed once after the API account ran out of credit). Changes made along the
way, beyond this plan:

- The Reader and Planner treat a model that refuses or runs out of output tokens as a failed
  attempt (previously the run stalled in `planning`).
- `python -m bench run --resume DIR` continues a stopped run without repeating or re-paying
  finished trials; an API refusal now ends a run cleanly with its report.
- The CUAD loader matches the real files: column capitalization, file-name spellings, and
  contract types taken from the PDF folders (25 types).
- 6 clean twins rather than about 8 (the mock contract system holds three contracts).

## How the work runs

- **Branch.** All work happens on a new branch, `m12-evidence`, cut from `main`. Nothing is
  committed or pushed without your permission. I'll ask at the end of each step, with the diff and
  test output ready.
- **Each step ends green.** `make check` (ruff, mypy, pytest) passes, plus any extra check the step
  names. If verification fails twice on a step, I stop and report instead of looping, as the
  roadmap requires.
- **Gates.** Steps marked **⛔ gate** need your answer before they start: a download, a live call,
  or spending money.
- **Two independent tracks.** Steps A1–A4 (anchor) and B1–B5 (benchmark) don't touch the same files,
  so they could run in separate sessions in parallel. By default I do them in this order in one
  session.
- **Already uncommitted on `main`:** `docs/roadmap.md`, `docs/m12-design.md`, `docs/m12-plan.md`.
  I'd move them onto the branch as its first commit, with your permission.

## Track A: audit-head anchor

### A1. Anchor core and file sink
- **Files:**
  - `core/audit.py`: `AnchorPoint`, the `anchor=` parameter on `verify_chain` and
    `AuditLog.verify`, and `anchored_seq` / `events_after_anchor` on `ChainVerification`.
  - New `core/anchor.py`: `AnchorRecord`, `AnchorSink`, `EvidenceCheck`, `Anchorer`, `check_anchor`.
  - New `anchoring/file_sink.py`.
- **Tests:** `tests/unit/test_anchor.py` and additions to `test_audit.py`. They cover:
  - an anchor in the middle of the chain is matched;
  - truncation below the anchored event is detected;
  - a rewritten event at the anchored point is detected;
  - events after the anchor are counted, not treated as failure;
  - `expected_head` behaves exactly as before;
  - the digest covers tenant, run, event number and hash, so changing any of them breaks it;
  - the file sink survives a reload and refuses a second, different anchor for the same run.
- **Done when:** `make check` passes.

### A2. Anchor when a run ends
- **Files:**
  - `engine/orchestrator.py`: an `on_finished` hook, called after `close_for_run` in `_finish`
    and `cancel`.
  - `api/wiring.py`, `api/main.py`: `GAWE_ANCHOR_SINKS`.
  - New `scripts/anchor_backfill.py` and `make anchor-backfill`.
- **Tests:**
  - For each ending (completed, failed, denied, cancelled), the anchor is the last event, including
    `approval.closed`.
  - A sink that raises leaves the run's status and audit log unchanged and increments
    `gawe.anchor.failures`.
  - With no setting, anchoring is off.
  - Backfill anchors only finished runs that have no anchor, and running it twice changes nothing.
- **Done when:** `make check` passes and `make test-integration` still passes on Postgres.

### A3. Verify route, docs and threat model
- **Files:** `api/service.py`, `api/schemas.py`, `api/app.py` (the `anchor` block, plus the
  optional `anchor_seq` / `anchor_head` query parameters), `docs/api.md`, and
  `docs/threat-model.md` (T8 cites the new tests; "external anchoring" leaves "Not covered yet").
- **Tests:**
  - every anchor status (`none`, `matched`, `mismatch`, `evidence_invalid`, `unavailable`);
  - an auditor's own copy overrides the stored one;
  - another tenant's run still answers 404;
  - errors never echo the hash that was sent;
  - adversarial: truncate a finished run's chain in the store, and verify reports a mismatch.
- **Done when:** `make check` and `make check-docs` pass.

### A4. Signed-timestamp sink and UI line
- **Files:**
  - `pyproject.toml`: the `anchor` extra (`cryptography`, `asn1crypto`), also included in `dev`.
  - New `anchoring/rfc3161.py` and `anchoring/tsa_certs/` (pinned DigiCert and Sectigo certificate
    chains).
  - `ui/app/runs/[id]/audit/VerifyButton.tsx`, `ui/lib/api-types.ts`.
- **Tests,** all offline, using a recorded reply:
  - a valid token is accepted;
  - a token for a different digest is refused;
  - a token signed by an unpinned certificate is refused;
  - a tampered token is refused;
  - an unreachable authority gives `unavailable`, not a crash;
  - UI: a formatting test for the anchor line.
- **⛔ gate:** recording the test fixtures needs **one live timestamp request per authority**. It
  sends a SHA-256 digest of a fixed test record, nothing else, the same as the Phase 2 check.
  Installing the two new dependencies also needs your OK at that point.
- **Done when:** `make check`, `make ui-check` and `make check-docs` pass.

## Track B: benchmark harness

### B1. Harness skeleton and budget
- **Files:**
  - New `bench/` package with `cli.py`, `budget.py` and `prices.yaml` (Sonnet 5.5 and Opus 5.5,
    "checked on" 2026-10-29).
  - `pyproject.toml` (mypy and ruff cover `bench`), `Makefile` (`bench-estimate`, `bench`,
    `bench-data`), `.gitignore` (`.bench-data/`, `bench-results/`).
  - `tests/bench/conftest.py`, which makes any real provider or token counter raise.
- **Tests:**
  - `run` refuses without `--max-usd`, and refuses an unknown model;
  - `CountingProvider` counts every call, including failed Reader attempts;
  - the stop before a call is exact at the boundary;
  - a budget stop writes `budget_stopped` and marks the results incomplete;
  - all start-or-refuse cases in section 4 (input over cap, expected over cap, ceiling over cap
    with and without `--accept-ceiling-over-cap`).
- **Done when:** `make check` passes.

### B2. CUAD loader, clause mapping and sample
- **Files:** `bench/cuad/fetch.py` (download and hash check, not run yet), `labels.py`,
  `mapping.py`, `sample.py`, `profile.py` (the presence-only Reader profile, checked by the same
  schema-safety rules as the app profiles).
- **Fixtures:** a tiny `master_clauses.csv` in CUAD's shape and three short synthetic contracts in
  `tests/bench/fixtures/`.
- **Tests:**
  - labels are read for scored and partial kinds;
  - contracts over the 200,000-character limit are excluded and listed;
  - sampling is the same for the same seed and different for a different seed;
  - the top-up guarantees at least 10 positives per scored kind where the data allows;
  - the presence profile passes `check_reader_schema`, and nothing under `gawe` imports `bench`
    (a test checks the import graph).
- **Done when:** `make check` passes.

### B3. Reader-accuracy run and metrics
- **Files:** `bench/metrics.py` (precision, recall and F1 per kind and micro-averaged, bootstrap
  interval, flip rate), and the CUAD part of `bench/cli.py`.
- **Tests,** using `FakeProvider` with scripted findings:
  - metrics checked against hand-computed values;
  - a Reader error counts as "flagged nothing" and adds to the failure rate;
  - recall for the `production` profile is labelled a lower bound;
  - results are written as JSON Lines as they happen, and contain no document text.
- **Done when:** `make check` passes.

### B4. Injection corpus and runner
- **Files:**
  - `bench/injection/corpus/`: about 24 attack documents and 8 clean twins, all synthetic, using
    `.example` domains. `C-1003` is reused.
  - `bench/injection/cases.yaml` and `bench/injection/runner.py`.
- **Tests,** using `FakeProvider` scripted to be fooled or not:
  - each outcome is classified correctly (Reader fooled, Planner fooled, stopped by validator /
    policy deny / pending approval);
  - "unauthorized step ran" comes from the ledger alone;
  - a planted canary in a CRM summary counts as "attacker text persisted";
  - the harness never approves anything, and a paused run is cancelled;
  - each trial gets a fresh ledger;
  - Wilson intervals are checked against known values;
  - every case's goal matchers are valid, and every attack has its technique and goal labelled.
- **Done when:** `make check` passes.

### B5. Free count, trial and report
- **Files:** the `estimate` and `--trial` commands in `bench/cli.py`, the token counter in
  `bench/budget.py`, `bench/report.py`, and a README section on how to run the benchmark.
- **Tests:**
  - estimate arithmetic (input, margin, ceiling, expected total) checked against a fake counter;
  - `--estimate-from` refuses a file that doesn't match the sample;
  - the report renders from a fixture `report.json`;
  - every number in `report.md` comes from `report.json`;
  - the "what this does not prove" section is always present.
- **Done when:** `make check` passes.

## Phase 4 to 5: live runs and verification

| Step | What happens | Your input |
|---|---|---|
| L1 | `make bench-data`: download CUAD (about 106 MB from Zenodo) and pin its hash | **⛔ gate:** approve the download |
| L2 | `make bench-estimate`: free token count, then print input cost and ceiling | **⛔ gate:** approve it (free, but live API calls) |
| L3 | Paid trial, capped at $2 | **⛔ gate:** approve. You see the expected total and how often calls hit the token cap |
| L4 | Full run: `make bench MAX_USD=<your cap>` | **⛔ gate:** you set the cap |
| L5 | Read the report together. Record the results and any policy findings | You decide whether it goes into `docs/` |
| V | Final checks: `make check`, `make check-docs`, `make ui-check`, `make test-integration`, `make test-schema`; update `docs/roadmap.md`, `README.md` and `docs/architecture.md` | Commit and merge only with your permission |

## Not in this plan

- Policy changes for internal email and low-level grants (decision 6).
- Changes to the Reader's effort setting or token cap (decision 9).
- AgentDojo, the Batch API, object-lock storage, and grouping many heads into one timestamp
  (design section 5).

## Rough size

| Step | Size |
|---|---|
| A1 | Medium |
| A2 | Small |
| A3 | Medium |
| A4 | Medium (certificate checking is the fiddly part) |
| B1 | Medium |
| B2 | Medium |
| B3 | Small |
| B4 | Large (writing the corpus is most of it) |
| B5 | Medium |

Each step is one reviewable commit.
