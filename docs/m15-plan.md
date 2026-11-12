# M15 build plan

Status: **done; S1–S4 implemented and verified** (2026-11-12).
Builds the [approved design](m15-design.md). All four design recommendations were approved:
strict address boundaries, exact requester matching with denial, declared variable provenance
plus shared-state sequence tests, and a separate milestone for audit-backed combinations.
Plan approved by the owner; S1–S4 were committed by the owner, who closed M15 on 2026-11-12.

## How the work runs

Work only on `m15-policy-edge-cases`. The owner commits; do not commit automatically. Preserve
existing staged work. Steps run in order and each ends with a diff summary, actual check output
and a suggested commit message, then a stop for owner review. Do not edit the roadmap or unrelated
features. No live model calls, API spend, history metadata, new signing keys or audit migrations.

At every checkpoint run:

```sh
PATH="$PWD/.venv/bin:$PATH" make check
PATH="$PWD/.venv/bin:$PATH" make check-docs
```

Use focused tests during development before the full checks. If verification fails twice on the
milestone, stop and report the failures instead of continuing a repair loop. Record substantive
changes to this plan/design; obtain a new decision if the approved security semantics must change.

## S1. Address boundaries and independent per-rule fuzzing

- `src/gawe/core/policy/rules.py`: enforce the approved single plain-mailbox contract. Consume the
  whole input; reject control characters, whitespace, recipient lists/display names, malformed
  labels and trailing dots. Preserve plus addressing, uppercase ASCII domains and supported Unicode
  local parts. Canonicalize international domains with strict IDNA2008 validation and no compatibility
  mapping that turns look-alikes into an internal domain. Apply identical domain normalization to
  configured operands. Invalid resolved domain operands cause evaluation errors, not dropped entries.
- Declare the IDNA dependency directly in `pyproject.toml`; choose and test explicit normalization
  settings. Do not rely on an incidental transitive dependency. Verify curated Unicode/ASCII pairs
  and rejection behavior against the approved contract before broadening generated cases.
- Add `tests/adversarial/test_policy_boundaries.py` and small test-only helpers/fixtures as needed.
  Each case carries an independently assigned outcome, expected matched rule-id set and parse label.
  Expected values may use simple threshold arithmetic or explicit case-kind labels; never use the
  production parser, operators, rule matcher or benchmark matcher as the expected-value oracle.
- Cover every existing rule in both apps and the example fixture using the one-sentence boundaries
  in design section 2.2. Generate both sides of numeric/enum/list boundaries, overlapping rules,
  missing/ill-typed operands and metadata selector mismatches. Treat schema/RBAC failures explicitly.
- Include recipient lists, display names, subdomains, look-alikes, trailing dots/newlines, empty labels,
  hyphen boundaries and case changes. Legitimate plus, uppercase and international-domain controls
  must remain allowed when configured internal. Do not expand shipped tenant allowlists.
- Compare the actual parser result with the independent parse label and compare signed engine
  decisions with expected outcome and rule ids. Add a coverage guard requiring every shipped rule
  to have a boundary family. Configure a fixed seed and deterministic Hypothesis settings; no
  persistent example database and no randomized deep run in `make check`.
- Update existing parser/unit/M14 property expectations only where the approved parser contract
  changes them. Preserve the independent unreadability tests, including schema-bypass properties.
- Add a threat-model control for malformed recipient interpretation, naming tests that now exist.

Done when the reported newline/label defects have regressions, every current rule has boundary
coverage, legitimate controls pass, and both checks pass. Stop for review.

Suggested commit: `test(policy): enforce explicit address and rule boundaries`

## S2. Deny grants to anyone other than the reported requester

- `src/gawe/apps/it_access/__init__.py`: expose `request.requester_email` from this run's persisted
  Reader findings. Missing, empty, non-string or unparseable values leave the variable unavailable
  so grant evaluation fails closed. Do not obtain it from the grant ledger or proposed arguments.
- `src/gawe/apps/it_access/policies.yaml`: add `requester-mismatch-denied` using `email: {ne:
  $request.requester_email}`. Compare exact strings, without trimming, lowercasing, decoding aliases
  or stripping plus tags. Keep the existing external, admin and production gates intact.
- Extend app tests with same requester allowed, different requester denied, invalid findings denied,
  case-only differences denied, and identical tricky addresses retaining their expected outcomes.
  Prove a correct requester still needs approval for admin/prod and external accounts still deny.
- Update direct engine fixtures to provide an intentional reported-requester context. Keep coverage
  for absent requester; do not populate the variable from the proposed grant in tests that are
  supposed to detect mismatches. Add the new rule to boundary and M14 variable coverage guards.
- `bench/injection/cases.yaml`: change only the three lateral-grant planner expectations from `none`
  to `policy_deny`; reader expectations stay `none`. Update the isolated goal-policy expectation
  using clean reported-requester context, and add explicit matched/mismatched context tests.
- Update benchmark tests and explanation so the distinction is visible: the rule stops a fooled
  Planner but cannot stop a fooled Reader choosing the attacker's requester. Preserve other goals.
- Update the threat model for requester substitution, with real tests and the remaining Reader limit.
- Run the scripted benchmark with the project virtualenv and compare every trial to the approved
  suite expectations, not merely the command exit status. Expected aggregate: 7 executions,
  8 denials, 23 approvals across 38 trials. Diagnose any other change rather than relabelling it.

Done when the three intended planner outcomes change, clean controls and all other expectations
hold, both checks pass, and benchmark output is recorded. Stop for review.

Suggested commit: `feat(it-access): deny grants to a different reported requester`

## S3. Check policy-variable provenance at startup

- Add tool write-resource declarations to `ToolManifest`, distinguishing absent metadata from an
  explicitly empty set. Require every registered version to declare writes at application assembly;
  reject absent metadata, invalid resource names and read-class tools declaring writes.
- Add typed source/variable declarations to `AppBundle`, with validation in a focused policy
  provenance module. Separate fixed configuration, frozen per-run findings and system-resource
  leaves from derived variables. Represent dependencies explicitly rather than a `trusted` flag.
- Traverse dependencies and reject unknown names, cycles, empty provenance and overlap with any
  registered tool version's writes. Dot-separated parent/descendant resources overlap; sibling
  fields do not. Do not accept arbitrary globs or treat unknown writes as empty.
- Require declarations for all referenced variable operands and validate every declared variable,
  including currently unused definitions. Fail assembly before serving runs. An empty declaration
  set is valid only when policy has no variable references and every tool has explicit metadata.
- Declare current app sources and write scopes from design section 4. Keep actual variable values
  supplied by the app callbacks. Clearly document that this checks application contracts rather
  than proving arbitrary Python data flow or guaranteeing Reader truth.
- Update all application assembly fixtures and manifest helpers that need declarations. Search for
  every AppBundle/ToolManifest constructor and file-loader entry point; do not leave an application
  entry point silently bypassing provenance validation. Pure grammar tests may remain isolated.
- Add focused tests for missing declarations, unknown/cyclic dependencies, transitive overlap,
  namespace overlap, safe sibling resources and an unsafe older registered tool version. Include
  a negative fixture in which a write to CLM deal values invalidates `review.large_deals`.
- Verify declared safe callbacks remain unchanged after representative real mock writes, with
  fixed findings. Add threat-model documentation of metadata trust and its limitations.

Done when each rejected case has coverage, both apps assemble with explicit declarations, existing
outcomes are unchanged from S2, and both checks pass. Stop for review.

Suggested commit: `feat(policy): validate variable sources against tool writes`

## S4. Shared-state sequence fuzzing and final verification

- Refactor `bench/injection/run.py` narrowly so tests can supply a reusable environment containing
  a bundle, stores, mocks and ledger. Keep fresh isolation as the normal `drive()`/CLI behavior.
- Add `tests/adversarial/test_policy_sequences.py`: for every one of the 38 scripted case/mode
  pairs, generate deterministic allowed-action prefixes, initially 0–6 actions and 10 examples.
  Execute prefixes through the real service/executor with known honest findings and valid roles.
  Require each chosen action to be allowed and complete; do not filter away blocked prefixes.
- Reuse those exact mock objects and stores for the attack. Exercise previous-run prefixes and
  within-run prefixes where feasible. Use distinct step/idempotency keys. Recreate the environment
  for each generated example, not between its prefix and attack.
- Recompute variables against the same fixed findings before and after prefixes and assert that
  declared non-tool-writable values do not change. Compare attack outcomes against the approved
  suite, covering every goal/mode and ruling out unexplained weakening.
- Separate prefix effects from the attack's success classification by a ledger offset; prefix state
  persists but a harmless earlier write must not be counted as attacker success. Test this isolation.
- Include a deliberately stateful test fixture whose allowed write changes a later policy input
  despite a false source declaration. Assert the tripwire detects this, proving that the harness
  tests shared state and does not merely agree with fresh-state expectations.
- Add references to the sequence and provenance tests in the threat model. Preserve the boundary
  tests and M14 properties; this tripwire complements startup declarations, not replaces them.
- Run the full checks and `PATH="$PWD/.venv/bin:$PATH" python -m bench scripted`; compare every
  recorded case/mode with its approved expectation. Record the results directory, commit, dirty
  status and complete check output. No model provider or paid calls may be constructed.
- Review the cumulative diff for scope, privacy, generated files and silent expectation changes.
  Mark both milestone documents done, with the all-approved decisions, any build changes and exact
  verification results. Record history combinations as split out and cross-workflow joins as waiting.

Done when sequence tests detect the deliberately unsafe fixture, all actual attack expectations
hold, both checks pass, and completion documents are accurate. Stop for final owner review.

Suggested commit: `test(policy): detect gate drift after allowed action sequences`

## Progress and deferred work

- Phase 2: all four design recommendations approved 2026-11-12.
- Phase 3: approved by the owner.
- S1: reviewed and committed by the owner.
- S2: reviewed and committed by the owner.
- S3: reviewed and committed by the owner.
- S4: reviewed and committed by the owner; M15 closed 2026-11-12.
- Audit-backed within-app combinations: split into a separate milestone by owner decision.
- Cross-workflow combinations: waiting for a shared system-of-record subject key.

S1 checkpoint: 191 new boundary cases; full checks passed with 1513 passed and 39 skipped in
64.84s, ruff and mypy passed (97 source files), and documentation checks resolved 78 references
with no problems. Full output is in `bench-results/m15-s1-check.txt`. The design build record
records the explicit IDNA settings and dependency. No design departures; S2–S4 remain pending.

S2 checkpoint: 1540 passed, 39 skipped in 65.21s; ruff/mypy passed and documentation checks
resolved 83 references. Full output: `bench-results/m15-s2-check.txt`. Scripted run
`20261112T213839Z-scripted` matched all 38 expectations (7 executed, 8 denied, 23 approval pending).
The design records its commit/dirty metadata. The results page preserves the old benchmark and
explains the three approved planner-mode changes. S3 and S4 have not begun.


S3 checkpoint: explicit tool writes and typed variable-source declarations are checked at startup,
including the live-smoke entry point. Added 73 provenance cases and updated assembly fixtures.
Final checks: 1613 passed, 39 skipped in 64.41s; ruff/mypy passed (98 source files); documentation
checks resolved 88 references. Full output: `bench-results/m15-s3-check.txt`. The design records
one initial fixture failure and its correction. No design departures; S4 has not begun.


S4 checkpoint: shared-state tests cover all 38 scripted case/mode pairs across runs and all 26
planner cases within runs, with 0–6 allowed prefix actions, 10 generated examples and two explicit
controls per property. A misdeclared stateful fixture proves both variable drift and weakened
outcomes are detected; a separate regression proves prior legitimate effects cannot count as
attack success. Empty prefixes skip execution rather than submitting a rejected empty plan.
The owner approved continuation after that fixture error triggered the two-failure stop rule.

Final checks: 1681 passed, 39 skipped in 100.61s; ruff/mypy passed (98 source files); documentation
checks resolved 92 references. Full output: `bench-results/m15-s4-check.txt`. Scripted run
`20261112T215545Z-scripted` matched every one of 38 case/mode expectations and execution flags:
7 executed, 8 denied, 23 approval pending. Commit `a6713939b273344873aa0409a72f4fa544946c9f`,
dirty tree, no model calls. Detailed verification and scope review are in the design build record.
No security-semantic departures. Approved M15 work is done; audit-backed combinations remain
split out and cross-workflow joins remain waiting.
