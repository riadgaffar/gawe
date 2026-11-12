# M14 build plan

Status: **done** (2026-11-12). All steps implemented, verified and reviewed; merged to `main`.
Builds the [approved design](m14-design.md). Audit markers are deferred by the approved decision.
Plan approved 2026-11-12. Implementation and verification are complete.

## How the work runs

- Work only on `m14-policy-load-checks`; the branch already exists. Do not commit or switch to main.
- Steps run in order. At the end of each step, stop with the diff summary, actual check output
  and a suggested one-line commit message. The user reviews and commits.
- Run both commands with the project's virtualenv on the path at each step:
  `PATH="$PWD/.venv/bin:$PATH" make check` and
  `PATH="$PWD/.venv/bin:$PATH" make check-docs`.
- Do not touch `docs/roadmap.md` or unrelated roadmap candidates; the roadmap is updated after review. No model
  calls or benchmark runs. Keep any new related findings in the design's limits appendix.
- Record progress and any design departures in these documents. Mark them done only when all
  three steps are implemented and verified. An implementation problem that requires expanding
  the approved design must be brought back for a decision.

## S1. Validate tool and argument references at application load

Changes:

- `src/gawe/core/policy/rules.py`: add a validator for parsed rules against a sequence of
  manifests; make `load_rules(path, *, manifests)` require manifests and invoke that validator.
  Leave `parse_rules(text)` as grammar validation, explicitly documenting that boundary.
- Resolve exact/glob tool selectors with the same case-sensitive matching as evaluation.
  Reject unknown names and unmatched globs before applying metadata selectors. Intersect with
  side-effect and risk selectors; reject an empty candidate set. Without a tool selector,
  check all manifests selected by metadata (all manifests for an args-only rule).
- Validate every dotted path on every candidate tool using explicit `properties`. Accept declared
  optional fields and enum-only leaves. Reject empty path components, absent properties,
  non-object traversal and paths requiring unsupported schema structure. Numeric path components
  remain object keys, never array indexes. Errors name rule, tool/version, path and cause.
- Implement only the schema-path subset in design section 2; do not resolve references or infer
  fields from additional/pattern properties. Test unsupported structure on the traversed path,
  unrelated structures outside it, and leaf constraints that do not affect declaration.
- `src/gawe/api/wiring.py`: pass `bundle.manifests` at assembly, after manifest registration, covering all
  executable versions (the S1 review correction authorized 2026-11-12).
  Keep schema syntax validation in the registry and preserve existing tool selection semantics.
- Update all `load_rules` call sites found by repository-wide search, including fixture flow,
  adversarial/golden, app and benchmark tests. Use each app's manifests or fixture `TOOLS.values()`.
  No file loader retains a silent manifest-validation bypass.

Tests:

- Extend `tests/unit/test_policy_rules.py` with exact names, globs, metadata-only and args-only
  selectors; no matches; combined filters; multiple matches where one tool lacks the path;
  nested/optional paths; empty components; arrays versus numeric object keys; enum-only leaves;
  and the conservative unsupported-schema boundary. Check useful error context.
- Add coverage loading Contract Review, IT Access and the example fixture with their own schemas.
- Add an assembly regression in app/wiring tests: rename `risk_level` in an otherwise valid
  Contract Review manifest and verify `RuleConfigError` prevents assembly. Include a successful
  unchanged bundle with multiple compatible versions; reject drift in either an older pinned
  version or the latest version.
- Confirm all existing golden policy and app tests retain their outcomes.

Done when both checks pass, all shipped policies load, and the renamed-argument startup regression
fails closed. Mark S1 complete and stop for review.

Suggested commit: `feat(policy): validate loaded rules against registered tool schemas`

## S2. Reject allow checks that can match unreadable values

Changes:

- `src/gawe/core/policy/rules.py`: enforce an explicit allow-safe operator set in `Rule` validation.
  Reject `ne`, `not_in`, `domain_not_in`, literal `exists: false`, and variable-backed `exists`.
  Reject unsafe checks even when another conjunct appears to guard them. An operator added later
  is not automatically safe for allow rules.
- Keep `exists: true`, equality, positive membership, numeric comparisons and positive domain
  membership legal under existing operand validation. Keep dynamic operand resolution at runtime.
- Preserve approval/deny negative checks and their empty-list behavior. Update the module
  explanation to state the conditions under which unreadability cannot widen authorization.

Tests:

- Extend `tests/unit/test_policy_rules.py` to cover every rejected class via YAML parsing and
  direct `Rule` construction, including literal empty/nonempty lists and variable operands.
  Test a guarded unsafe conjunct is still rejected and error context identifies the rule/check.
- Cover accepted positive operators with literal and variable operands, including `exists: true`.
  Show positive membership on an empty runtime list does not match. Confirm unresolved or
  ill-typed variable operands still deny through the engine when evaluated.
- Retain or add approval/deny examples for negative checks and `exists: false`/variable-backed
  exists. Test the safe-operator classification cannot silently accept a newly added operator.
- Reload all three shipped policies and preserve their existing golden outcomes.

Done when both checks pass, every unsafe allow class is rejected, and stricter effects retain
existing semantics. Mark S2 complete and stop for review.

Suggested commit: `feat(policy): reject allow predicates that match unreadable values`

## S3. Prove the shipped-policy property and document the controls

Changes:

- `pyproject.toml`: add Hypothesis to dev dependencies and install it into the existing virtualenv
  if needed. Do not upgrade unrelated dependencies.
- Add `tests/adversarial/test_policy_unreadable.py` with one independent property test parametrized
  across the two shipped app policies and the example fixture, using real schemas and signed
  engine decisions. Configure deterministic generation and disable the persistent example database.
- Use explicit valid action templates for every tool, valid roles and variables. Parameterize
  mutation targets so every tool and policy-referenced path is exercised rather than relying on
  random selection to reach them. Check template/target coverage against loaded manifests/rules
  so a new tool or path cannot silently fall outside the test.
- Generate missing keys, nulls and wrong JSON types with a bounded finite JSON strategy. Separate
  schema-invalid mutations from schema-valid malformed domain strings, constructed independently
  of the production domain parser. Preserve other conjuncts so the relevant checks are reached.
- Exercise populated and empty list variables; cover unresolved and wrong-type variables only
  where the matching predicates reach them. Assert schema failures report `engine.invalid_args`,
  variable errors report `engine.error`, and unreadable domains receive the expected approval or
  deny. Verify decision signatures. Add readable allowed controls to catch blanket denial.
- Keep the precise property boundary from design section 4: plain free text with no applicable
  parsing predicate is not classified as unreadable. Nested synthetic cases are covered in loader
  tests; current shipped paths are flat. Do not expand into the roadmap's per-rule fuzzing work.
- `docs/threat-model.md`: add a row for stale tool/argument references and unsafe allow checks,
  citing actual tests added in S1–S3. State limits: schema-valid false facts, optional fields with
  unconditional allows, and semantic validation outside policy remain outside this control.
- Mark design and plan done after verification, recording any changes during implementation and
  preserving the decision to defer audit markers.

Verification:

- Run the property tests directly while developing, then run the full required checks once the
  change is ready. Inspect failures rather than weakening assertions or filtering counterexamples.
- Review the final diff for scope, accidental policy behavior changes, privacy and generated files.
  Confirm there are no commits by the agent, no roadmap edits and no public-repo changes.
- Report check output, coverage of the rejected cases, design decisions and remaining limits.

Done when the property test passes for every shipped policy, both required checks pass, threat-model
references resolve, and both milestone documents record completion. Stop for final user review.

Suggested commit: `test(policy): enforce unreadable-input safety across shipped policies`

## Progress

- Phase 2 design: approved 2026-11-12, audit markers deferred.
- Phase 3 plan: approved 2026-11-12.
- S1: complete and committed by the user, including the all-version review fix.
- S2: complete and committed by the user.
- S3: complete (2026-11-12); awaiting final user review/commit.

S1 verification: `make check` passed with 1115 passed and 39 skipped (50.64s); ruff and mypy
passed (97 source files). `make check-docs` checked 65 references with zero problems.
The 55 new cases are grouped in `tests/unit/test_policy_references.py`, including assembly and
shipped-policy loading. The design build record notes the additional smoke-script caller and
the pinned-version recovery gap, now closed by the authorized review correction. S2 and S3 are complete.

S2 verification: `make check` passed (1181 passed, 39 skipped in 49.55s), ruff and mypy passed
(97 source files), and `make check-docs` checked 65 references with zero problems. The design
build record summarizes the 65 additional cases and the operator-test helper adjustment.

S3 verification: 93 new cases passed; full `make check` passed with 1274 passed and 39 skipped
in 55.96s, ruff passed, and mypy found no issues in 97 source files. `make check-docs` checked
72 references with zero problems. `git diff --check` passed. The design build record documents
the generated-input boundaries, target coverage guard, final check output and completion status.
No changes to policy YAML, the roadmap, the public repo, or signing/audit payloads were needed.
