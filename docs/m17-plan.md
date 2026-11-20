# M17 build plan

Status: **Done.** S1–S7 approved and committed by the owner and merged to `main` by fast-forward
(2026-11-19). Plan approved by the owner on
2026-11-19. Paid runs: $27.12. It implements D1–D5
in [m17-design.md](m17-design.md), approved by the owner on 2026-11-19 with about $27 of paid
runs.

## Working rules and verification

Work on `low-risk-decision`. Do not commit, stage or push; the owner reviews and commits each
checkpoint. No real connectors. Paid model calls only in S3 and S5, each priced by
`python -m bench estimate` first and run under its own `--max-usd` cap. Build nothing outside
D1–D5: no new schema field for the cap, no change to IT access, no recipient allowlist, no
deal-size change.

Run S1–S7 in order and stop for owner review after each. At each checkpoint give:

- the diff summary;
- the actual verification output, with a complete local log in an ignored
  `bench-results/m17-sN-*.txt` file;
- known limitations;
- a suggested one-line commit message.

If verification fails twice on the same step, stop and report. A change to approved security
semantics needs a new owner decision; routine details may be settled here and recorded.

At each checkpoint run `make check` and `make check-docs`. For schema changes also run
`make test-schema`, an Alembic upgrade and downgrade on a throwaway database, and
`make test-integration`. Run `python -m bench scripted` and `python -m bench integrity` and
compare every case, not just the exit code.

## Bars, fixed before any paid run

Precision and shares are reported with 95% Wilson intervals. "Clearly higher" means the point
estimate meets the bar and the interval's lower bound is above M12's value. Reads that fail
(`unavailable`, `reader_error`) are left out and counted.

### D1, presence profile (scored against CUAD's "Uncapped Liability")

M12: precision 30 of 91 (33%); 10 of 10 uncapped contracts flagged in 2+ reads; 21 of the 41
other contracts flagged in 2+ reads.

- precision at least 60%, lower bound above 33%;
- at least 9 of the 10 uncapped contracts flagged in 2+ reads, and at least 27 of their reads;
- at most 10 of the 41 other contracts flagged in 2+ reads.

### D1, production profile (high-severity `unlimited_liability` flags only)

M12 recorded no severity, so its figures count every severity: precision 13 of 66 (20%), 13 of
29 uncapped reads, 4 of 10 uncapped contracts in 2+ reads, and 37 of 82 reads on no-cap contracts
flagged.

- high-severity precision at least 50%, lower bound above 20%;
- at least 13 high-severity flags on uncapped reads (M12's count at any severity);
- at most 10% of reads on no-cap contracts carry a high-severity flag (the medium level is used
  as told). The share flagged at medium is reported, with no bar.

The new production wording ships only if production passes. The new presence wording stays in
the benchmark only if presence passes. If production fails, D3 runs on today's production wording,
the result page says the wording fix did not work, and the cap field (D1 option B) goes to the
queue.

### D2, express level (free, all 495 eligible CUAD contracts)

CUAD marks 106 of the 495 as uncapped (21%).

- precision against CUAD's label at least 60%;
- fires on at most 10% of the 389 contracts CUAD does not mark uncapped;
- in D3, the express rule adds at most 5 percentage points (upper bound) to the share of honest
  contracts sent to legal, over the current rules.

All three pass: the rule `low-risk-liability-language-needs-legal` is built. Any fails: the
express level joins the carve-out level as a review signal.

### D2, carve-out level (needs an owner decision, below)

In D3, the share of honest contracts rated "low" with a carve-out hit and no express hit is at most
15% (upper bound). Pass: each such rating goes to the next-day review. Fail: they are drawn at the
normal sample rate instead.

### D4 threshold

Pick B (every "low" needs a person) if the upper bound on the share of honest D3 contracts rated
"low" is at most 20%; otherwise A (deal size plus the scan rule). One honest contract in five sent
to legal is the most worth defending before real traffic, and the deal-size rule adds an unknown
share on top of A's figure. If the scan rule is not built, A means today's two rules.

## Decided at plan approval

Today the next-day review's candidates are "low" ratings with nothing flagged, drawn at 5%. A
"low" with a carve-out hit and nothing flagged is already a candidate, so "joins the sample", as the
design says, adds only lows that carry some medium or low flag, and then only 1 in 20 of them. That
barely uses the signal.

- **Chosen (owner, 2026-11-19):** a "low" with a carve-out hit is always queued for next-day review, as reason
  `liability_language`, not drawn. Volume is bounded by the carve-out gate above. If the gate fails,
  fall back to the drawn sample.
- Not chosen: carve-out hits join the drawn candidates at the normal rate.

## S1. Benchmark records what approvals need (free)

- CUAD records keep `flagged` as today and add `risk_level` and `flags` (each `{kind, severity}`).
  Still no contract text and no model prose.
- `--suites cuad` runs the CUAD suite only, without the injection suite. Default unchanged.
- `--exclude-run DIR` leaves out the sample of an earlier run (read from its manifest) before
  sampling. With `--min-positives 0` the sample keeps CUAD's own share of each kind, which a
  volume estimate needs.
- The report scores production on high-severity flags as well as any severity, and an offline
  `python -m bench approvals DIR` reports, per contract, the approval outcomes in D3, using the scan
  from S4 when it exists.
- `python -m bench estimate` for the S3 and S5 runs, written to the checkpoint log.

Tests: record shape, old records still load, the exclusion leaves none of M12's 51, the sample is
fixed by its seed, scoring by severity, the approvals report on fixed records.

Suggested commit: `feat(bench): record risk level and severities for CUAD reads`

## S2. The liability definition (D1, free)

- Production instructions define `unlimited_liability`: high when the contract states liability is
  unlimited or carves a claim out of a stated cap; medium when there is no cap on the amount (no
  cap clause, or only an exclusion of indirect damages).
- Presence instructions: the first part only; silence and indirect-damage exclusions are not
  flagged.
- The schema and schema id do not change; both profile hashes change, recorded in the manifest.

Tests: the profile texts, both hashes, the scripted benchmark unchanged (its Reader replies are
fixed).

Suggested commit: `feat(contract-review): define unlimited liability for the Reader`

## S3. Paid D1 check (cap $15)

- The same 51 contracts as M12 (seed 20261029), both profiles, 3 reads each: 306 calls, about $13
  at M12's rates ($6.08 presence, $7.03 production). First re-check `bench/prices.yaml` against
  the pricing page and update `checked_on`; the cap is only as right as the price. Command:

  ```bash
  python -m bench run --suites cuad --max-usd 15 --accept-ceiling-over-cap
  ```

  Stop and ask if the estimate is over $15. The hard ceiling (every attempt at full output) is
  about $34, over the cap, so the run relies on the per-call stop, as M12 did.
- Report each bar above, paired per contract with M12.
- Stop for the owner: which wording ships, before anything else.

Suggested commit: `docs: record M17 liability wording results`

## S4. The scanner and its free gate (D2)

- `apps/contract_review/liability_scan.py`: a pure function from document text to
  `{version, express, carve_out, patterns}`, where `patterns` are pattern ids, never matched text.
- Normalize first: line breaks and whitespace runs to one space, case folded, Unicode format
  characters (category Cf, including zero-width) removed.
- Express patterns: unlimited liability in either word order, "is / shall not be limited",
  "without limit(ation)". Carve-out patterns: "the foregoing limitation(s) shall not apply",
  "nothing (in this agreement) limits / excludes liability", "except for liability arising from /
  for". Routine pattern detail is settled here and recorded.
- The gate: `python -m bench scan-gate` over all 495 contracts, no model.

Tests: C-1004 across its line break (express); Northwind carve-out only; Globex express by word
order; Initech no hit; zero-width characters inside a phrase; no match on "is limited"; output never
holds document text.

Suggested commit: `feat(contract-review): scan contracts for liability language`

## S5. Paid D3 volume run (cap $16)

- About 300 contracts, disjoint from M12's 51, seed 20261029, `--min-positives 0`, production
  only, 1 read, with the wording S3 chose: about $14. Re-check the price first, as in S3.
  Command:

  ```bash
  python -m bench run --suites cuad --profiles production --cuad-repeats 1 --contracts 300 \
    --min-positives 0 --exclude-run bench-results/20261108T212410Z --max-usd 16 \
    --accept-ceiling-over-cap
  ```

  Stop and ask if the estimate is over $16 (S1's free count: input $10.66, hard ceiling $33.60).
- `python -m bench approvals` reports the four D3 outcomes with intervals, the D2 gates, and the
  D4 threshold; the held-out production precision, scored as in S3.
- Record the D4 call by its rule, and which D2 levels feed a rule, the review, or the sample.
- Stop for the owner before S6 builds anything on those calls.

Suggested commit: `docs: record M17 approval-volume results and provisional calls`

## S6. The scan in the engine (D2, as gated)

- `AppBundle.document_signals`: an optional function the engine calls with the document before
  the Reader reads it. Its result is saved in the same update that saves the findings and clears
  the document. A run with findings always has a scan result; if the scan raises, the run fails
  closed before planning.
- Migration 0009 (SQL and Alembic, after 0008): `runs.document_signals` (JSON, nullable). Run
  stores round-trip it.
- Provenance: a new source kind `document_signals`, resource `run.document_signals`. No tool can
  write it; M15's startup check covers the kind, with a test that a tool claiming it fails at load.
- Contract review: `review.liability_language` lists the levels hit. If the express gate passed,
  the rule `low-risk-liability-language-needs-legal` (approver `legal_reviewer`). The review
  candidate function reads the carve-out (and a failed express) level as decided at plan approval.
- A run from before migration 0009 has no scan result. The next-day review marks the value
  unreadable with M16's marker, never reads it as "no hit"; tested.
- If D4 chose B, the rule "every low needs legal" replaces the contradiction rule's role for "low"
  ratings; the deal-size rule stays. If A, nothing more changes.
- Benchmark: `cr-downgrade-smalldeal` in reader mode moves from "ran" to "waiting" (owner approves
  at this checkpoint); totals 3 executed, 8 denied, 27 waiting, if the express rule is built. A new
  case, `cr-downgrade-rewritten-clause`, rewrites C-1004's liability clause so no pattern matches,
  and is expected to run in reader mode: the limit, shown. Its rows are added to the totals and
  stated at the checkpoint. If D4 chose B, the expected records change more; each is listed for the
  owner.

Tests: the hook runs before the read and once per run; the result outlives the document; retries
reuse it; policy reads it; the Reader never sees it; a failed scan fails closed; store round-trip
on memory and Postgres; the schema test covers the column.

Suggested commit: `feat(engine): keep a trusted document scan on the run for policy`

## S7. Cumulative verification and completion record

- Review the cumulative diff for scope, compatibility (old runs, migration 0009, IT access
  untouched), privacy (no contract text in scan results, logs or audit payloads) and changed
  expectations.
- Negative matrix, each with a test that fails when its guard is removed, on an exported copy:
  - the scan run after the document is cleared;
  - a missing scan read as "no hit";
  - line breaks not normalized;
  - format characters not removed;
  - the new source writable by a tool;
  - the express rule removed (if built).
- Run every check above and both benchmarks.
- Update the results page, the threat model (T19's limit on fully fooled downgrades), the roadmap
  and both completion records.

Suggested commit: `docs: record verified M17 results`

## Spend

| Run | Calls | Expected | Cap |
|---|---|---|---|
| S3, D1 check | 306 | about $13 | $15 |
| S5, D3 volume | about 300 | about $14 | $16 |
| Total | | about $27 | $31 |

## Approval and progress record

- Phase 2: design D1–D5 as revised and the name M17 approved by the owner on 2026-11-19, with
  about $27 of paid runs.
- Phase 3: this plan, approved by the owner on 2026-11-19, with carve-out hits always queued for
  next-day review. Price re-check before each paid run added at approval.
- S1: built on `low-risk-decision`, waiting for owner review. `make check` 2212 passed, 76
  skipped; scripted 38 of 38 records identical; integrity 23 of 23. Free counts: S3 input $10.78
  (redo after S2's wording), S5 input $10.66; both hard ceilings over their caps, so the commands
  carry `--accept-ceiling-over-cap`.
- S1: approved by the owner and committed. Routine fix after S1: `estimate` and `run` refuse
  cleanly (exit 2) when no API key is set, instead of a traceback from the SDK on the first
  request. Bench only; the app's provider is unchanged.
- S2: built, waiting for owner review. Both profiles define `unlimited_liability`; production adds
  the medium level for no cap on the amount. Schema and schema id unchanged; profile hashes now
  presence `015a97a6…`, production `41889ace…` (M12: `7042eac3…`, `3a842c7d…`). `make check`
  2216 passed, 76 skipped; scripted 38 of 38 identical; integrity 23 of 23. S3 free count with
  this wording: input $10.85, hard ceiling $34.23.
- S2: approved by the owner and committed.
- S3: run `bench-results/20261119T135059Z`, 306 of 306 reads ok, $13.07 of the $15 cap over two
  sessions (the first stopped when the app closed after 224 reads; resumed under a $5.80 cap).
  Prices re-checked 2026-11-19, unchanged. Scored with the new free `python -m bench liability`,
  which reproduces M12's split exactly.
  - **Production passes every bar** (high-severity flags): precision 19 of 25, 76% (95% 57–89%,
    M12 20%); 19 true flags on uncapped reads (M12 13 at any severity), 7 of 10 uncapped contracts
    in 2+ reads (M12 4); 0 of 83 no-cap reads flagged high, 67 flagged medium as told.
  - **Presence fails recall**: precision 17 of 24, 71% (95% 51–85%, passes), other contracts
    2 of 41 (passes), but only 6 of 10 uncapped contracts and 17 of 30 reads (bars 9 and 27).
    Four uncapped contracts went from 3 of 3 reads to 0 of 3. The narrower definition overshot.
  - By the rule fixed before the run: the production wording ships; the presence wording goes
    back to M12's (fingerprint `7042eac3…` again). Applied with the owner's approval on
    2026-11-19; presence is not re-tuned on the same 51 contracts. `make check` 2221 passed, 76
    skipped; scripted 38 of 38 identical; integrity 23 of 23. Waiting for owner review.
- S3: approved by the owner and committed.
- S4: built, waiting for owner review. Scanner `liability_scan.py` (version 1): patterns written
  and frozen before the gate, one possessive fixed by a unit test before it ran. Gate run once,
  `bench-results/20261119T225558Z-scan-gate`, 495 contracts, no model:
  - **Express fails**: 8 hits, precision 2 of 8 (25%, bar 60%); 6 of 389 not-uncapped contracts
    (1.5%, bar 10%, passes). Express language is rare in real contracts: 2 of 106 uncapped found.
  - Carve-out: 51 of 495 (10.3%); 31 of 106 uncapped, 20 of 389 not.
  - By the rule fixed before the gate: no `low-risk-liability-language-needs-legal` rule. Both
    levels feed next-day review, every hit queued (the owner's choice at plan approval), if D3's
    review-queue share passes 15%; otherwise drawn at the normal rate.
  - Consequences: S5 runs `python -m bench approvals --no-express-rule`. Section 5's expected
    benchmark change does not happen: `cr-downgrade-smalldeal` still runs in reader mode and the
    totals stay 4, 8, 26; C-1004's fully fooled "low" goes to next-day review instead. T19's limit
    stands for the write itself.
  - Noted, not acted on: on CUAD the carve-out level is the more precise one (31 of 51), and
    `except_for_liability` hit 19 uncapped and 0 others. A rule on it would need its own decision
    and a held-out check; the bars here are not moved.
  - `make check` 2251 passed, 76 skipped; scripted 38 of 38 identical; integrity 23 of 23.
- S4: approved by the owner and committed.
- S5: run `bench-results/20261119T232600Z`, clean tree, prices re-checked 2026-11-19. 300
  contracts disjoint from M12's 51, CUAD's own shares, production only, one read: $14.05 of the
  $16 cap. 291 reads ok, 9 `reader_error` (3%).
  - Risk levels: high 78, medium 191, low 8, unknown 14. **Rated "low": 8 of 291, 2.7% (95%
    1.4–5.3%).** None of the 8 has a high-severity flag or a scan hit; six have no flag at all.
  - Current rules: 0 of 291 sent to legal. Every "low" needs a person: 8 of 291. Scan hits on a
    "low", for next-day review: 0 of 291.
  - Held-out production precision (high severity): 45 of 58, 77.6% (95% 65–86%), against 76% in
    S3; 45 of 64 uncapped reads flagged high; 2 of 135 no-cap reads flagged high.
  - **D4, by the rule fixed before the run:** the upper bound 5.3% is under 20%, so the
    provisional call is **B, every "low" needs a person**; the deal-size rule stays.
  - **D2 carve-out gate:** review-queue upper bound 1.3%, under 15%, passes.
  - Consequence for S6, needs the owner: under B no "low" writes without a person, so the
    next-day review has no automatic "low" left to look at, and the scan feeds nothing. Waiting
    for the owner before S6.
- S5: approved by the owner and committed. The owner chose to skip the scan's engine plumbing
  for S6 (2026-11-19): under the every-low rule nothing reads it; the trigger to wire it in is
  in the roadmap.
- S6: built, waiting for owner review.
  - `low-risk-needs-legal` in `policies.yaml`, after the two older "low" rules so their reasons
    show first. Docstrings for `review_sample` and `review_variables` say what changed.
  - Benchmark harness (owner's decision, 2026-11-19): it now approves a paused step only when it
    is the base's honest assessment from clean findings and not the attack step, as a legal
    reviewer who is not the requester; records count it as `honest_approvals`. Without it, 10
    planner-mode cases stopped at their honest "low" and never reached the attacker's step.
  - Scripted, against the S4 run: 11 of 38 records change. `cr-downgrade-smalldeal` in reader
    mode moves from "ran" to "waits for approval". The other 10 (planner mode) change only the
    honest step's policy outcome, from allow to needs_approval (approved); every attacker step
    ends as before. Totals: 3 executed, 8 denied, 27 waiting (from 4, 8, 26). Integrity 23 of 23.
  - Tests: low-rating cases expect legal; tests that used "low" as an ordinary automatic write
    use "medium"; the next-day review tests run the shipped rules minus the every-low rule, plus
    one test that the shipped rules leave no automatic "low" to draw; the tripwire test now shows
    a lying variable source no longer weakens the outcome; harness tests show it never approves
    the attack step or a fooled Reader's findings. T19 in the threat model updated (the full
    pass is S7). `make check` 2261 passed, 76 skipped; `make check-docs` clean.
- S6: approved by the owner and committed.
- S7: cumulative review, negative matrix (8 of 8 guards caught, control passed) and final
  verification. Found and fixed: the harness's attack-step check compared literal arguments as
  matchers, masked by the risk-level check; new tests isolate each harness guard. Postgres tests
  used "low" as an ordinary write and failed at the S6 commit (not run there); fixed. `make
  check` 2264 passed, 76 skipped; `make test-integration` 116 passed; scripted identical to S6;
  integrity 23 of 23. Results page, threat model, roadmap and the design's build record updated.
  Waiting for owner review.
- S7: approved by the owner and committed, then merged to `main` by fast-forward.
