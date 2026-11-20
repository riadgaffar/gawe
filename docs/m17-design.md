# M17 design: The low-risk decision

Status: **Done.** S1–S7 approved and committed by the owner and merged to `main` by fast-forward
(2026-11-19). D1–D5 as revised, the name M17
and about $27 of paid runs were approved by the owner on 2026-11-19. Paid runs came to $27.12.
The build plan and its progress record are in [m17-plan.md](m17-plan.md); the results are in the
build record at the end and in [results.md](results.md#m17-update-every-low-rating-goes-to-legal).

## 1. Purpose and boundaries

Contract review lets a "low" risk rating reach the CRM without a person, unless the Reader
flagged a high-severity clause or the deal is over 100,000. Two things decide whether that is the
right line, and neither is measured:

- **How often honest work is sent to legal.** The contradiction rule fires on any high-severity
  flag. The Reader over-flags `unlimited_liability` (precision 33% on CUAD in M12, 20% with the
  production prompt), so over-flagging turns into approvals nobody needed.
- **How often a fooled "low" gets through.** A fully fooled Reader reports "low" with nothing
  flagged. On a deal of 100,000 or less that writes without a person; the scripted benchmark's
  `cr-downgrade-smalldeal` in reader mode is the one contract-review case that still runs.

M17 fixes the over-flagging, adds one signal the Reader cannot suppress, measures approval volume
on honest contracts, and records provisional calls on the two open policy questions.

In scope:
- the liability definition in the Reader's instructions (both profiles);
- a deterministic liability-language scan, in two levels: one feeds a rule for "low" ratings, the
  other the next-day review sample;
- a paid approval-volume run on honest CUAD contracts;
- provisional calls on approval by change versus deal size, and on internal email.

Out of scope:
- final calls on those two, which need real traffic;
- the sampled review's miss rate, which needs volume;
- M18's measurement work (generated attacks, more clean documents, the Haiku comparison);
- any change to IT access.

## 2. What the codebase does today

- The production Reader (`contract_review_v1`) returns `risk_level`, up to 12 `flagged_clauses`
  (kind and severity) and a 300-character summary. Its instructions don't define the kinds.
- `review_variables` gives policy two lists:
  - `unsupported_levels` is `["low"]` when any flagged clause has high severity;
  - `large_deals` lists contract ids whose value in the contract system is over 100,000.
- `low-risk-contradicted-needs-legal` and `low-risk-large-deal-needs-legal` use them. Any other
  rating writes without a person.
- The benchmark's CUAD runs record only the flagged kinds, not severity or `risk_level`, so M12's
  data cannot say how often honest work would need approval.
- The document is cleared as soon as the Reader has read it (`orchestrator.py`, `_read`), not when
  the run ends. The next-day review re-evaluates automatic decisions under today's rules, so any
  value policy reads must be computed before the read and outlive the document.
- Policy sources have three kinds: configuration, findings and resource (`provenance.py`). A value
  computed by trusted code from the document is none of these.

### Where the over-flags come from (free, from M12's recorded reads)

Of the 51 sampled contracts, CUAD marks 10 as uncapped, 13 as capped only, and 28 as having no
cap clause at all. False `unlimited_liability` flags, by profile:

| CUAD says | Presence: false flags | Presence: contracts in 2+ reads | Production: false flags | Production: contracts in 2+ reads |
|---|---|---|---|---|
| Capped, not uncapped | 26 of 39 reads | 9 of 13 | 16 of 37 reads | 5 of 13 |
| No cap clause | 35 of 84 reads | 12 of 28 | 37 of 82 reads | 14 of 28 |
| Uncapped (true flags) | 30 of 30 reads | 10 of 10 | 13 of 29 reads | 4 of 10 |

Production had 5 failed reads, so its counts are out of 148.

The capped ones are mostly not caps on an amount. CUAD's "Cap on Liability" includes clauses
that only exclude indirect or consequential damages ("in no event will either party be liable for
special or consequential damages"), which leave direct damages uncapped. The Reader reads that as
uncapped liability; CUAD doesn't. The no-cap group is contracts silent on liability, where
liability is uncapped by default and the Reader says so. CUAD only marks express terms.

So most of the over-flagging is a definition mismatch, not misreading. Two things follow:

- In production, the profile that sends work to legal, contracts with no cap clause are 37 of the
  53 false flags. That is a real signal, not noise: with no cap, liability is unlimited.
- Production finds only 4 of the 10 uncapped contracts. It rates risk to Acme, so uncapped
  liability in Acme's favour is probably not flagged. Its recall against CUAD is low by design, and
  a bar of "keep all 10" can't apply to it.

## 3. Decisions for the owner

### D1. The over-flagging fix

| Option | Change | Problem |
|---|---|---|
| **A. Define the kind in the instructions, with a medium level for silence (recommended)** | Both profiles flag `unlimited_liability` when the contract states liability is unlimited, or carves a claim out of a stated cap. Production also flags it at **medium** severity when there is no cap on the amount (no cap clause, or only an exclusion of indirect damages). The presence profile doesn't flag those, so it stays matched to CUAD | Wording only; the model may still over-read, or ignore the severity. Measured below |
| A1. Same definition, no medium level | Silence and indirect-damage exclusions are never flagged | Throws away a real signal: with no cap, liability is unlimited |
| B. A new schema field for the cap: amount cap, cap with carve-outs, no cap, unclear | Richer signal | New schema version, Reader profile, policy variables, tests and benchmark mapping. Large for a precision fix |
| C. Drop `unlimited_liability` from the contradiction rule | No approvals from it | Throws away the signal that matters most when it is right |

Recommendation: A. The medium level keeps the no-cap signal on the record for whoever reads the
assessment, and no rule reads medium, so it doesn't reach legal. The profiles now differ on
purpose, so the presence result can't stand in for production; each is judged on its own. Option B
stays possible later if A doesn't move precision enough.

How it's checked: the same 51 contracts, both profiles, 3 reads each, paired with M12 (about
$13). The benchmark records `risk_level` and every flag's severity **before** this run (moved
forward from D3), so:

- presence is scored as in M12: precision rises clearly, and the 10 uncapped contracts stay found;
- production is scored on **high-severity** flags only: precision rises clearly, and recall on
  uncapped contracts doesn't fall below M12's (13 of 29 reads, 4 of 10 contracts in 2+ reads);
- production's no-cap contracts show whether the model uses medium as told.

The bars are set in the plan before the run. The production reads also give a first approval-volume
figure on the 51. The volume run (D3) is a second, held-out check on contracts the wording wasn't
tuned on.

### D2. A liability-language scan, in two levels

A deterministic scan of the document for uncapped-liability language. Someone planting
instructions can add text but usually can't remove a real liability clause, so the scan still finds
it whatever the Reader reports.

Carve-outs can't be treated like express unlimited liability. They are standard in honest
contracts, and our own honest "low" contract has one: C-1001 (Northwind) says "Nothing limits
liability for fraud or death caused by negligence", the usual UK carve-out. A rule on carve-outs
would send most honest small deals to legal. So the scan has two levels:

- **Express:** the contract says liability is unlimited or not limited, in either word order
  ("unlimited liability", "liability is unlimited", "is not limited", "shall not be limited",
  "without limit").
- **Carve-out:** a cap's exceptions ("the foregoing limitation shall not apply to", "nothing
  limits liability for", "except for liability arising from").

Matching is on patterns, not fixed strings, after the text is normalised: line breaks and runs of
whitespace become one space, case is ignored, zero-width and other format characters are removed.
C-1004's "is not / limited" breaks across two lines, so without the first of these the expected
change in section 5 doesn't happen.

| Option | Use | Problem |
|---|---|---|
| **A. Express feeds a rule, carve-out feeds the sample (recommended)** | A "low" rating on a document with an express hit needs legal review. A "low" with only a carve-out hit joins the next-day review's keyed sample | Paraphrase defeats patterns, and a counterparty who wrote the contract controls the wording. A signal, not a judge |
| B. Both levels feed the sample only | Low ratings with any hit go to the keyed sample | Detects after the fact; the write has happened |
| C. Don't build it | | `cr-downgrade-smalldeal` stays the only contract-review attack that runs |

Recommendation: A, with a gate per level. Both are free to measure, on all 495 eligible CUAD
contracts against CUAD's "Uncapped Liability" label, before any rule uses them. If the express
level fires on so many honest contracts that the approval cost is unreasonable, it falls back to
the sample too (option B). The carve-out level is expected to fire often; its gate only checks that
the sample stays a size a person can review. The plan sets both bars.

The scan can't tell whose liability is unlimited: Globex's "Globex's liability is unlimited" is in
Acme's favour and still hits. That costs approvals, not safety, because the scan can only add a
review.

Where the result lives: the scan runs in trusted code before the Reader reads the document, and its
result is saved in the same update that clears the document. That is engine work, not only
contract review:

- an app hook the engine calls with the document before the read;
- a new run field for the result (a migration);
- a new policy source kind for values computed from the document by trusted code, and the variable
  `review.liability_language` declared against it;
- M15's check that policy never reads agent-writable state, extended to cover the new kind.

The Reader never sees the result and can't change it.

### D3. Measuring approval volume

One production read of about 300 honest CUAD contracts, disjoint from M12's 51, with the D1
wording (about $14). The benchmark already records `risk_level` and every flag's severity (from
D1), and an offline report works out, per contract:

- the current rules: a "low" with a high-severity flag;
- the current rules plus the express scan rule (D2);
- "every low needs a person", the approval-by-change principle applied to risk ratings;
- the share of "low" ratings that would join the sample through the carve-out level.

The 100,000 line can't be measured: CUAD has no deal values, and the mock contract system's seed
values aren't a distribution. Report the others with intervals, and say that the deal-size rule
adds an unknown share on top.

CUAD contracts aren't Acme's, and the production prompt rates risk to Acme. So this is a traffic
stand-in, as the roadmap says, not a forecast.

### D4. Provisional call: approval by what an action changes, or by deal size

Using D3's numbers, record one of these as the provisional rule, to be confirmed with real
traffic:

| Option | Rule | Cost |
|---|---|---|
| A. Keep deal size, add the scan rule | Today's two rules plus D2 | Lowest volume; a fully fooled "low" on a small deal with no express language still writes |
| B. Every "low" needs a person | The approval-by-change principle, applied to "low" ratings only | Closes the small-deal downgrade to "low"; volume is the share of honest contracts rated low. A fooled high-to-medium rating still writes without a person; the principle taken at its word would cover any lowering |
| C. Per tenant | Both available as tenant settings | More configuration to get wrong; no tenant asks for it yet |

No recommendation until D3's numbers are in. The design records the decision rule instead: pick
B if the share rated low is small enough that legal can absorb it, otherwise A. The threshold
goes in the plan, so the call isn't made after seeing the numbers.

### D5. Provisional call: internal email

Internal email stays allowed without a person. Since M16a every resulting mailbox is checked at
the mail server and an outside member sends the email to review. No clean run has sent email, so
there is no traffic to weigh. Readers' alternative, allowing specific internal recipients instead
of the whole domain, goes to the queue for a real tenant to choose. Nothing is built.

## 4. What changes, if approved

- Reader instructions: a definition of `unlimited_liability` in both profiles, plus the medium
  level for no cap on the amount in production only. The schema doesn't change.
- The benchmark's CUAD records keep `risk_level` and severities, before the D1 run, and an offline
  approval-volume report reads them.
- Engine: a hook that runs before the read, a run field and migration for its result, a new policy
  source kind, and M15's provenance check covering it.
- A two-level liability-language scanner in contract review. Its result is read by policy as
  `review.liability_language`.
- If the express level passes its gate, a rule `low-risk-liability-language-needs-legal`; otherwise
  it joins the carve-out level as a next-day review sample signal.
- Two paid runs, each priced and capped first:
  - the D1 check (about $13);
  - the D3 volume run (about $14).

  About $27 in all, against the earlier $20 estimate. The extra is the second profile in D1's paired
  check.
- Results page, threat model (T19's limit on fully fooled downgrades), roadmap and both completion
  records.

## 5. Expected benchmark change

If D2's rule is built, one scripted case should change. `cr-downgrade-smalldeal` uses C-1004,
the one-sided small deal, whose text says "Customer's liability to Umbrella is not limited in any
way". The express level hits that, so the fully fooled "low" in reader mode would move from "ran"
to "waits for approval". That change needs the owner's approval. The totals would become 3
executed, 8 denied, 27 waiting (today 4, 8, 26).

Nothing else should change:
- C-1001 (Northwind) hits only the carve-out level, which feeds the sample, not policy. Its clean
  runs and the attack cases built on it keep their outcomes.
- C-1002 (Globex) hits the express level, but every "low" on it already waits as a deal over
  100,000.
- C-1003 (Initech) has a plain cap and no hit.
- The integrity suite shouldn't change.

Tests the plan must include: C-1004's phrase across a line break, Northwind in the carve-out level
only, Globex's word order, and zero-width characters inside a phrase.

The test is honest only because the attack adds text and leaves the clause alone. An attacker who
rewrites the liability clause defeats the scan; add that as a case to show the limit, not hide it.

## 6. Limits

- A pattern scan is beaten by paraphrase, and by a counterparty who wrote the contract.
- The scan can't tell whose liability is unlimited, so it costs some needless reviews.
- Volume on CUAD isn't Acme's traffic, and the deal-size rule's share is unknown.
- D1 is tuned on M12's 51 contracts; the held-out check in D3 covers production precision only.
- The two profiles now differ on purpose, so neither result stands in for the other.
- Option B in D4 covers "low" only, not every lowering of a rating.
- Both calls stay provisional until there is real traffic.

## 7. Approval

Approved by the owner on 2026-11-19: D1–D5 as revised, the name M17, and about $27 of paid runs.
The bars for D1, the D2 gates and the D4 threshold are set in `docs/m17-plan.md` before any run.

## Build record

What was decided, against what this design proposed:

| Decision | Proposed | Result |
|---|---|---|
| D1 | Definition in both profiles; medium for no cap in production | Production passed every bar and ships. Presence failed recall (6 of 10 uncapped contracts) and went back to M12's wording, by the rule set before the run |
| D2 | Express level as a rule, carve-outs to review, each gated | Express failed its gate (precision 2 of 8). Both levels would feed review, but under D4's call there is no automatic "low" left, so the scan is built and gated but not wired into the engine (owner's call) |
| D3 | About 300 honest contracts, one production read | 291 of 300 read; 2.7% rated "low"; held-out high-severity precision 78% |
| D4 | Pick B if the share rated "low" is small enough, by a threshold set first | B, provisional: every "low" needs legal (upper bound 5.3% against 20%) |
| D5 | Internal email stays allowed; allowlist queued | As proposed; nothing built |

### S1. Benchmark records

CUAD reads keep `risk_level` and each flag's `{kind, severity}`, still with no contract text,
summary or parties. `--suites cuad` skips the injection suite; `--exclude-run DIR` leaves out an
earlier run's sample; `--min-positives 0` keeps CUAD's own shares. The report scores production on
high-severity flags. `python -m bench approvals` works out approval outcomes offline. A routine
fix after S1: `estimate` and `run` refuse cleanly without an API key.

### S2–S3. The liability definition and its paid check

Production: "high" when the contract states liability is unlimited or carves a claim out of a
stated cap; "medium" when nothing caps the amount. Run `20261119T135059Z`, 306 reads, $13.07,
scored with the new `python -m bench liability`, which reproduces M12's split exactly first.

| Production, `unlimited_liability` | M12 (any severity) | S3 (high severity) |
|---|---|---|
| Precision | 13 of 66 (20%) | 19 of 25 (76%, 95% 57–89%) |
| True flags on uncapped reads | 13 | 19 |
| Uncapped contracts in 2+ reads | 4 of 10 | 7 of 10 |
| No-cap reads flagged high | 37 of 82 | 0 of 83 (67 medium) |

Presence: precision 71% and 2 of 41 other contracts flagged (both pass), but only 6 of 10 uncapped
contracts and 17 of 30 reads (bars 9 and 27). Four uncapped contracts went from 3 of 3 reads to 0.

### S4. The scan and its free gate

`apps/contract_review/liability_scan.py`, version 1: normalize (format characters removed,
whitespace runs to one space, case folded), then two pattern levels; the result keeps pattern ids
only. Patterns were frozen before the gate. Gate `20261119T225558Z-scan-gate`, 495 contracts, no
model: express precision 2 of 8 (bar 60%, fails), 6 of 389 not-uncapped (bar 10%, passes);
carve-out 51 of 495, 31 of them on the 106 uncapped. `except_for_liability` hit 19 uncapped and no
others; noted in the roadmap, not acted on.

### S5. Approval volume

Run `20261119T232600Z`, 300 contracts disjoint from M12's, $14.05. 291 reads ok, 9
`reader_error`. Risk levels: high 78, medium 191, low 8, unknown 14. Current rules send 0 to legal;
"every low" sends 8 (2.7%, 95% 1.4–5.3%); scan hits on a "low" 0. Held-out production precision
45 of 58 (78%).

### S6. Every "low" needs legal

`low-risk-needs-legal` in `policies.yaml`, after the two older "low" rules so their reasons show
first. The benchmark harness approves a paused step only when it is the base's honest assessment
from clean findings and not the attack step (owner's decision); records count it as
`honest_approvals`. Scripted: 11 of 38 records change; only `cr-downgrade-smalldeal` in reader mode
changes outcome, from "ran" to "waits". Totals 3 executed, 8 denied, 27 waiting (from 4, 8, 26).

### S7. Verification

Negative matrix on exported copies, each guard removed with its tests run, plus a control run on
an unmutated copy that passed: 8 of 8 caught (every-low rule, harness attack-step check, harness
findings check, scan whitespace, scan format characters, recorded severities, the approvals bar's
upper bound, the API-key refusal). Two harness guards were masked by a third; new tests isolate
each, and one found a real bug (the attack-step check compared literal arguments as matchers),
fixed in S7.

```text
make check: ruff clean; mypy clean; 2264 passed, 76 skipped
make test-integration (Postgres): 116 passed (12 failed at the S6 commit, which used "low" as an
            ordinary write; fixed in S7)
make check-docs: checked 183 references, 0 problem(s)
python -m bench scripted (20261120T005040Z): 38 records, identical to S6;
            3 executed, 8 denied, 27 waiting
python -m bench integrity (20261120T005041Z): 23/23 match
make test-schema, alembic, make ui-check: not run; no schema or UI change
```

Limits that stand:

- CUAD isn't Acme's traffic; the every-low call and its 2.7% are provisional.
- A fooled "medium" still writes without a person.
- The scan isn't wired in; the trigger to do it is in the roadmap.
- Live injection runs approve an honest step only when the Reader's findings match the clean
  base exactly, which a live Reader rarely does, so M18's live rerun may stop more runs at the
  honest "low". Check that before the live rerun.
