# M19 design: The Reader's summary limit

Status: **Done.** S1–S5 approved and committed by the owner and merged to `main` by fast-forward
(2026-11-22). D1–D4, the name M19 and about
$17 of paid runs were approved by the owner on 2026-11-22. Paid runs came to $15.98. The plan and
its progress record are in [m19-plan.md](m19-plan.md); results are in the build record at the end
and in [results.md](results.md#m19-update-the-readers-summary-limit-fixed).

## 1. Purpose and boundaries

The contract Reader's `summary` is capped at 300 characters by its schema, but its instructions
never say so, and they ask for the contract's main risk plus a note on any text aimed at AI
reviewers. When that doesn't fit, both attempts are rejected and the run fails closed: no
findings, no plan, nothing written (threat model T37). M18 measured what that costs:

- Sonnet's Reader failed on 11 of 201 live runs, including the small-contract downgrade attack 5
  of 5, so that attack's live outcome is still unmeasured, and one honest CUAD contract.
- Haiku's Reader failed on 40 of 130 attack runs and 79 of 306 CUAD reads. Every labelled failure
  was `summary:maxLength`. It is most of why Haiku failed its Reader bar.
- M17's 300-contract volume run had 9 Reader errors, probably the same limit.

M19 fixes it, measures the fix, and re-runs the Haiku comparison it distorted.

In scope: the contract Reader's instructions (production, and the benchmark's presence profile),
the CUAD runner's failure labels, and the paid checks below. Out of scope: other bounds, the IT
Reader (it had no summary failures), and any change to what policy reads.

## 2. What the codebase does today

- `contract_review_v1` schema: `summary` is a string of at most 300 characters. The Reader
  validates strictly and retries once; an over-long summary fails the attempt.
- The summary goes to the Planner (fenced as data) and can become `crm.write_assessment`'s
  `summary` argument (at most 500, a free-text field shown collapsed on the approval screen since
  M16b). T30 is about encoded text riding a bounded field like this into a message body.
- Production asks for "one or two plain sentences describing the contract and its main risk",
  then separately to "mention in the summary" any text aimed at automated reviewers. Presence asks
  for "one or two plain sentences naming the type of contract" and carries the same mention rule.
- The CUAD runner (`bench/cuad/run.py`) records `reader_error` but not why. The injection harness
  has labelled failures since M18.

## 3. Decisions for the owner

### D1. The fix

| Option | Change | Problem |
|---|---|---|
| **A. Say the limit (recommended)** | Instructions state "at most 300 characters", and say the injection note replaces detail rather than adding to it | The model may still run over; measured below |
| B. Raise the bound to 500, matching the tool argument | Fewer failures by construction | A new schema version; more room for T30's encoded payloads; the approval screen shows more model text |
| C. Truncate instead of rejecting | No failures | The Reader would accept output outside its schema. That undoes T37's control and the "invalid output is never accepted" property. Declined |

Recommendation: A. It changes wording only, keeps the bound and the schema id, and is the smallest
change that could work. If A doesn't bring failures down far enough (bar in the plan), B is next,
as its own decision.

### D2. Which profiles

| Option | Change | Problem |
|---|---|---|
| **A. Both, production and presence (recommended)** | The same sentence in both | Both fingerprints change, so both Sonnet baselines need re-reading (D3) |
| B. Production only | Smallest product change | Haiku's presence failures stay, so the Haiku comparison stays distorted |

Recommendation: A. Presence is benchmark-only, but the Haiku question can't be answered fairly
otherwise.

### D3. How it's checked

All on the same 51 contracts and the same suite, so everything stays paired:

| Run | What it answers | Expected |
|---|---|---|
| Sonnet, CUAD, both profiles, 3 reads | Reader errors with the new wording, and whether M17's D1 bars still hold on production | about $13 |
| Sonnet, attack suite × 5, plus the 5 labelled failures and C-2005 | Reader errors on attack runs; the small-deal downgrade judged live at last | about $3 |
| Haiku, CUAD both profiles and the suite | The M18 Haiku comparison again, paired against the new Sonnet reads | about $1.20 |
| Total | | about $17 |

The CUAD runner first gains the same failure labels the injection harness has (free), so every
remaining failure says why.

Options to spend less: production only on CUAD for Sonnet (about $7) loses the presence baseline
and so the Haiku presence comparison; dropping the Haiku re-run saves about $1.20 and leaves M18's
Haiku verdict standing on a known distortion.

### D4. What decides it

Fixed in the plan before any run: a bar on Reader errors (from 11 of 201 live, and 1 of 306 on
CUAD for Sonnet), that M17's production bars still pass, that no attack newly fools the model, and
the M18 Haiku bars re-applied unchanged.

## 4. What changes, if approved

- One instruction sentence in each contract Reader profile; schema and schema id unchanged.
- `reader_failures` labels on CUAD records.
- Three paid runs, each capped.
- Results page, threat model (T37's limit), roadmap and both completion records.

## 5. Expected changes to existing numbers

- The scripted benchmark doesn't change (its Reader replies are fixed).
- Both Reader fingerprints change, so later runs compare with M19's Sonnet reads, not M12's or
  M17's.

## 6. Limits

- A stated limit is a request, not a guarantee; the schema still enforces it, and some failures
  may remain.
- One date, one model version each.

## 7. Approval

Approved by the owner on 2026-11-22: D1–D4 as drafted, the name M19, and about $17 of paid runs.
The bars and caps are set in `docs/m19-plan.md` before any run.

## Build record

| Decision | Proposed | Result |
|---|---|---|
| D1 | State the limit in the instructions | Done: "at most 300 characters in all", and the injection note takes the place of detail. Schema and bound unchanged. Option B (a longer bound) wasn't needed |
| D2 | Both contract profiles | Done; both fingerprints changed |
| D3 | Paired checks on the same contracts and suite | Sonnet suite $2.56 and known failures $0.28; Sonnet CUAD $12.21; Haiku $0.93 |
| D4 | Bars fixed before the runs | Every Sonnet bar passes; Haiku fails the presence Reader bar again and fails honest IT planning |

| Reader errors | Before | After |
|---|---|---|
| Sonnet, attack suite | 10 of 130 (M18) | 0 of 130 |
| Sonnet, CUAD | 1 of 306 (M17) | 0 of 306 |
| Haiku, attack suite | 40 of 130 (M18) | 0 of 130 |
| Haiku, CUAD | 79 of 306 (M18) | 7 of 306 |

The small-contract downgrade, unmeasured live since M13, was judged 5 of 5: the model ignored the
payload and the Reader flagged it every time. Paired against the old reads, nothing else moved
(Sonnet F1 −0.2 points on presence, +0.7 on production; M17's liability bars still pass).

A correction: M18's results said most of Haiku's Reader gap was Reader errors. With the errors
gone the gap stayed (presence F1 −4.2 points, 95% −10.7 to +2.0), so that was wrong; the results
page now says so.

### S5. Verification

Review of the cumulative diff: one instruction change in the production Reader, the same in the
benchmark's presence profile, failure labels on CUAD records, tests and docs. Schema, policy and
engine unchanged.

Negative matrix on exported copies, after a control run that passed: 3 of 3 caught (the limit
removed from production; the replace-not-add rule removed from presence; CUAD failure labels
carrying text).

```text
make check: ruff clean; mypy clean; 2437 passed, 76 skipped
make test-integration (Postgres): 116 passed
make check-docs: checked 188 references, 0 problem(s)
python -m bench scripted (20261123T002702Z): 38 records, identical to M18's last run
python -m bench integrity: 23/23 match
make test-schema, alembic, make ui-check: not run; no schema or UI change
```

Limits that stand:

- A stated limit is a request; the schema still enforces it, and a document built to provoke a
  long answer can still make a read fail (T37).
- Haiku is still not good enough in either role, on this prompt and date.
