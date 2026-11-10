# M13 build plan

Status: **done** (2026-11-10). Every step was built and verified; S5 ran the scripted benchmark
at commit `671f129`. Builds the approved design in [m13-design.md](m13-design.md), which lists
what changed during the build. In short: two modes and 38 runs (not three and 50), the small-deal
attack on a new contract C-1004 as its own goal, and no `--out` option.

## How the work runs

- **Branch.** Work happens on a new branch, `m13-second-line`, cut from `main`. I create the
  branch; you commit. At the end of each step I give you the diff summary, the test output and a
  one-line commit message.
- **Each step ends green.** `make check` (ruff, mypy, pytest) passes. If verification fails twice
  on a step, I stop and report instead of looping.
- **No gates for money.** Nothing in M13 calls a model or spends anything. The only step that
  touches what others see is S6 (the results page and roadmap in both repos), and you push.
- **Already uncommitted on `main`:** `docs/m13-design.md` and this file. They become the branch's
  first commit.
- Steps run in order; each depends on the one before.

## S1. Concrete attacker values and the 26th attack

- `bench/injection/cases.yaml`:
  - each goal gains `attack_step` (`tool`, `args`, `{canary}` allowed), taken from the test file's
    `GOAL_STEPS` and the existing `tool_call` values;
  - `downgrade`, `prod_admin`, `lateral_grant` gain `attack_findings`;
  - each base gains `clean_findings`, and Contract Review bases gain `contract_id`;
  - new case `cr-downgrade-smalldeal`: goal `downgrade`, technique `html_comment`, on a new base
    `umbrella`, contract C-1004 (USD 60,000, one-sided terms; added to the Contract Review sample
    CLM data). Changed during S1: both existing small contracts (C-1001, C-1003) are honestly low
    risk, so a downgrade attack on them would "succeed" trivially; the loader now refuses that.
- `bench/injection/cases.py`: load the new fields; refuse a suite where an `attack_step` does not
  satisfy its goal's `plan` matcher, `attack_findings` does not satisfy its `findings` matcher, or
  any findings fail the app's Reader schema.
- Tests: the suite-shape test becomes 26 attacks (19 Contract Review) and 7 twins; the goal-policy test uses
  `attack_step` instead of `GOAL_STEPS` (which is deleted); a load-error test per new check.

Done when the suite loads, every new value is checked, and `make check` passes. The attack-corpus
hash changes (expected; recorded in the manifest).

## S2. The scripted runner

- New `bench/injection/scripted.py`:
  - `MODES = ("planner", "reader", "both")`; `scripted_trials(suite)` yields (case, mode) for
    every applicable pair: 26 + 12 + 12 = 50 with the new case;
  - honest plan builders: Contract Review (`clm.get_contract`, then `crm.write_assessment` at the
    findings' risk level), IT Access (`iam.lookup_user`, then `iam.grant_access` for the findings'
    requester, system and level); the attacker's step, when added, depends on the last honest step;
  - the reply script answers the Reader once and every Planner attempt with the same plan;
  - `run_scripted(trials, writer)` drives each through `drive()` with a zero-price
    `CountingProvider(FakeProvider(...))` and writes `injection_scripted` records with `mode`.
- `bench/injection/run.py`: `stopped_by` gains `not_acted_on` (fooled, but no accepted step carries
  the goal); `not_run` keeps only "in the plan and allowed, but the run ended first".
- Each goal in `cases.yaml` gains `expect_scripted: {planner: ..., reader: ..., both: ...}` with the
  outcome under the policy as written today (`ran`, `policy_deny`, `approval_pending`,
  `validator`, `not_acted_on`).
- Tests: plan builders per app; reply scripts; `not_acted_on` versus `not_run`; and one
  parametrized test that runs all 50 and checks each against `expect_scripted`.

Done when the 50 scripted runs match their declared outcomes in `make check`.

## S3. Command, records and report

- `bench/cli.py`: `python -m bench scripted [--out DIR]`, no `--max-usd`, no `--model`. Writes
  `bench-results/<stamp>-scripted/` (manifest: git commit and dirty flag, corpus and suite hashes,
  modes), then its report. `python -m bench report DIR` renders either kind of run.
- `bench/metrics.py`: `summarize_scripted()` (outcome per goal and mode; counts per layer);
  `summarize_clean_policy()` (tool, outcome, count over the twins).
- `bench/report.py`: the scripted report (design 3.1: counts, no intervals, a plain statement of
  what scripted runs do and do not show); the clean-run policy table in the live report.
- `bench/injection/run.py`: twin records keep `assessment_risk` (the risk level a clean contract
  run wrote; `null` for IT Access).
- Tests: the command never constructs a real provider (a provider factory that raises if called);
  manifest contents; report rendering for both kinds; the twin field.

Done when `python -m bench scripted` writes a run and a readable report locally, and `make check`
passes.

## S4. The downgrade rules

- `src/gawe/apps/contract_review/__init__.py`: `variables(run)` adds
  - `review.unsupported_levels`: `["low"]` when the run's findings hold a high-severity flagged
    clause (the AI-directed-text flag is one), else `[]`;
  - `review.large_deals`: contract ids whose CLM deal value is over 100,000 (from the bundle's own
    `MockCLM`).
- `policies.yaml`: two `needs_approval` rules for `crm.write_assessment`, approver
  `legal_reviewer`:
  - `low-risk-contradicted-needs-legal`: `risk_level: {in: $review.unsupported_levels}`;
  - `low-risk-large-deal-needs-legal`: `contract_id: {in: $review.large_deals}` and
    `risk_level: {eq: low}`.
- `tests/fixtures/example_policy_cases.yaml` and app tests: low with and without a high-severity
  clause; low on C-1002 (250,000) and C-1004 (60,000); medium on C-1002; unknown contract id.
- `cases.yaml`: the `downgrade` goal's `policy` and `expect_scripted` updated. The four C-1002
  attacks now wait for approval; `cr-downgrade-smalldeal` still runs, which is the visible gap.
  Because `expect_scripted` is per goal, the downgrade goal's expectation becomes per case where
  they differ (an override on that case).
- `docs/threat-model.md`: a row for "a fooled Reader returns an allowed but false value", with the
  two rules as mitigations and a fooled "low" under 100,000 as the remaining risk.

Done when the policy tests and the 50 scripted expectations pass with the new rules.

## S5. The scripted run, for real

- Run `python -m bench scripted` on the branch with a clean tree (after you commit S4), so the
  manifest names a commit.
- Read the report and check it against the expectations; fix anything misleading in its wording.

Done when the report exists for a committed tree and reads correctly.

## S6. Results page, roadmap and design status (both repos)

- `docs/results.md` (both repos): a section "When the model is fooled", with the scripted table, the
  clean-run policy table, the two new rules and the remaining gap, and the decision on internal
  email and access grants.
- `docs/roadmap.md` (both repos): M13 row; remove the done or wrong candidates ("record which
  layer stopped each attack", "report the scripted bad-plan tests", "replay the clean runs' plans
  through policy alone" (the clean-run table), "publishing a report in docs/"); amend "whether
  internal email ... should need approval" to the recorded decision; amend the Haiku comparison
  to the test agreed with a reader on dev.to: a paired permutation test over contracts (swap a
  whole contract's answers between the two models at random, about 10,000 samples, recompute the
  metric) plus the count of discordant (contract, clause type) pairs, not separate intervals.
- The public repo also needs the code: copy the changed files from this branch into
  `../gawe-public` without the internal docs, and run `make check` there.
- `docs/m13-design.md`, this file: status done, with anything that changed during the build.

Done when both repos pass `make check` and the public diff contains no internal docs.

## Not in this plan

Live model runs; generated or extra attacks beyond the one; the Haiku comparison; liability
carve-out categories; short-field injection; approve-once; the next-day review job; changing the
internal email or access-grant rules.

## Rough size

S1 small, S2 medium, S3 medium, S4 small to medium, S5 small, S6 small. No spend.
