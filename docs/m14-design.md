# M14 design: Policy that cannot drift silently

Status: **done** (2026-11-12). S1–S3 implemented, verified and reviewed; merged to `main`.
Phase 0 scope and Phase 1 findings are below. The user approved the proposed design, including
deferring audit markers. The build plan is in [m14-plan.md](m14-plan.md), approved at the separate
Phase 3 gate on 2026-11-12.

## 1. Scope and outcome

GAWE's rule remains: the model proposes, deterministic code decides. M14 closes two
policy-authoring gaps: stale tool/argument references and allow predicates that match absent
or unreadable values. An invalid policy must prevent application assembly with a useful error.
The two first candidates under "After M13" in `docs/roadmap.md` are the scope.

Work stays on `m14-policy-load-checks`. No commits, public-repo changes, roadmap edits, model
calls or live benchmark runs. Each implementation step stops for review with its diff summary,
actual `make check` output and a suggested commit message. The user commits.

## 2. Validate policies against the registered tools

Make file loading require manifests: `load_rules(path, *, manifests)` parses and checks the
policy. Keep `parse_rules(text)` as a grammar-level API for isolated rules tests; it does not
claim to validate an application's tool references. Add an explicit manifest-validation helper
for parsed rules. Every existing file-loader caller supplies its app or fixture manifests.
In `assemble`, load against `bundle.manifests` after registration. Validate every registered
version: new steps select the latest version, but resumed steps retain their pinned version.
Direct callers must likewise supply every executable version. No optional argument silently
skips checks in the application file loader. This corrects the initial latest-only design,
as authorized after S1 review on 2026-11-12.

For each rule:

1. If `when.tool` exists, match it against registered names with the same case-sensitive
   `fnmatch.fnmatchcase` used at evaluation. An exact name or glob matching no tools is an error,
   even if other selectors would hide the stale name.
2. Intersect with `side_effect_class` and `risk_tier`, when present. Without a tool selector,
   begin with all registered tools. Reject an empty final candidate set as a dead rule.
3. Every dotted argument path must be explicitly declared in **every** candidate tool's input
   schema. A glob does not mean that one successful tool is enough. A rule without a tool name
   is checked against every tool its metadata predicates select; an args-only rule checks all.
4. Walk `properties` one component at a time. Reject empty path components, missing properties,
   or traversal through a non-object. The root already declares `type: object`; intermediate
   nodes must explicitly declare `type: object`. Optional declared fields are allowed: declaration
   is not a promise that every runtime input contains the field. Leaf enum-only schemas work.
   Numeric components mean object keys, never array indexes, matching runtime `lookup`.

Bound the schema feature set deliberately: prove paths through explicit object/properties
schemas. `additionalProperties` and `patternProperties` do not establish a named field.
If a traversed schema needs `$ref`, composition (`allOf`, `anyOf`, `oneOf`), conditional schemas,
negation or other indirect structure to establish the path, reject with an unsupported-path
schema error instead of guessing. Do not fetch remote references or implement a general JSON
Schema resolver in M14. Unsupported constructs away from the path do not require rejection;
leaf value constraints do not need to be interpreted to prove the leaf's declaration.
Tests must pin this boundary, including no accidental acceptance of a path hidden in a union.
All shipped tool input schemas fit the proposed subset.

Errors identify the rule id, selector or tool name/version, argument path and cause. They do not
include runtime arguments or resolved variable values. Example: rule
`low-risk-large-deal-needs-legal`, tool `crm.write_assessment@1.0.0`, path `risk_level`:
property is not declared. This turns the motivating rename into a startup failure.

## 3. Reject unsafe allow predicates

Enforce in `Rule` validation so both file loading and direct rule construction apply it:

| Predicate in an allow rule | Decision |
|---|---|
| `ne`, `not_in`, `domain_not_in`, literal or variable operand | Reject |
| `exists: false` | Reject |
| `exists: $variable` | Reject: the runtime value may be false |
| `exists: true` | Accept |
| `eq`, `in`, numeric comparisons, `domain_in` | Accept with existing operand validation |

Reject an unsafe predicate even when another conjunct appears to guard it. This conservative
rule is easy to review and remains safe when conditions are edited. Do not try to prove
satisfiability across predicates. Approval and deny rules retain these operators unchanged.

All variable-backed negative membership checks are rejected for allows, regardless of whether a
particular run provides an empty or nonempty list. No variable declarations or startup sampling
are needed. Positive `in`/`domain_in` on an empty list cannot match and stay legal. Missing or
ill-typed variable operands continue to cause `engine.error` when evaluated. Dynamic variable
values are not resolved during loading. New operators must explicitly be classified as safe for
allow use; use an explicit safe set rather than allowing new operators by default.

Update the rules module's explanatory text: missing-value safety depends on these restrictions
and the engine's schema check. No matching semantics or shipped policy outcomes change.

## 4. A separate property test for shipped policies

Add Hypothesis as a dev dependency and one independent, parametrized property test covering
Contract Review, IT Access and `tests/fixtures/example_policies.yaml`, with their real manifests.
Use deterministic generation (`derandomize=True`, no persistent example database) for `make check`.
This test is separate from per-rule boundary examples; the broader roadmap fuzzing is out of scope.

Define the tested property precisely: for an otherwise eligible action, schema-invalid resolved
arguments, or an unreadable value at an applicable policy check, must not produce final `allow`.
Use real `RuleEngine.evaluate`, real manifests and valid roles, avoiding RBAC as an accidental
reason to pass. Verify the signed decision as well as the outcome. Keep known valid allowed
controls so a blanket deny cannot satisfy the suite unnoticed.

Generate mutations of known valid action templates, covering every shipped tool and every
policy-referenced argument path: missing keys, null, wrong JSON types, broken intermediate
objects where applicable, and malformed single-address values for domain checks. Generate
malformed addresses by construction (for example multiple @ signs, recipient lists, whitespace),
not by asking the production email parser which examples to retain. Generate bounded JSON values
without non-finite numbers so canonical hashing remains defined. Keep other conjuncts satisfied
when exercising a check. Vary applicable list variables between populated and empty lists;
exercise unresolved/wrong-type operands where evaluation reaches them. Assert `engine.invalid_args`
for schema-invalid mutations and the expected approval/deny protection for schema-valid malformed
domain values. Loader-focused tests cover nested synthetic schemas and all unsafe allow cases.

The quantifier is important: arbitrary free text is not "unreadable" merely because it resembles
an invalid address. `iam.lookup_user.email` has a string schema and no domain predicate; a string
such as `not-an-address` currently passes policy for that read tool. A broad metadata-only allow
also need not read optional fields. Do not claim this property proves semantic correctness of
all tool arguments or arbitrary future policies. Changing read-tool address validation, adding a
global runtime unreadable-value veto, or rejecting all optional policy fields would expand scope.
If the desired interpretation includes these cases, that needs an explicit design change.

## 5. Unreadable marker: decision for Phase 2

**Approved decision (2026-11-12): defer the marker beyond M14.** The two load-time checks and the property test
stand on their own. The marker is useful observability, but affects more than loading.

If included, propose a stable ordered list of check records with rule id, argument path,
operator and `unreadable: bool`, never the argument or operand value. Record only evaluated
checks in matched rules; a schema/RBAC early rejection has no matched-check records. A
short-circuited predicate is not evaluated, so absence must not be presented as "readable".
Readability is operator-specific: absent for existence, numeric for comparisons, parseable single
address for domain checks; null can be a legitimate scalar for equality. This needs its own
precise operator contract and tests rather than treating every non-match as unreadable.

Authenticate the records in the decision HMAC and carry them into `policy.decided` audit payloads.
That changes `PolicyDecision`, signer seal/verify, run serialization and the orchestrator audit
payload; API exposure needs a separate deliberate choice. Existing stored decisions use the v1
HMAC body. Adding a default field to that body would invalidate old approvals after restart.
Use an explicit decision version and preserve v1 verification, while issuing v2 decisions whose
marker fields are covered by the signature. Do not fall back to v1 verification after a v2 failure.
The audit chain would bind the extra metadata without storing arguments. Compatibility, tampering,
privacy and round-trip tests would be required. No database migration is assumed merely because
runs store decisions in JSONB; persisted-model compatibility must still be tested.

## 6. Verification and completion

Tests will cover unknown names, unmatched globs, empty metadata intersections, heterogeneous
multi-tool matches, nested/optional paths, unsupported schema structures, all rejected allow
predicates (including dynamic exists), allowed positive variable predicates, and unchanged
approval/deny semantics. Both app bundles and the example policy must load successfully.
An assembly test renames `risk_level` in the manifest and proves startup fails before a run.

Add a threat-model row for stale policy references and unsafe allows, naming tests only after
those tests exist. Run `PATH="$PWD/.venv/bin:$PATH" make check` and `make check-docs` at the
implementation checkpoints. Mark design and plan done only after implementation and verification,
recording any approved departures. Do not edit the roadmap.

## 7. Approval recorded

The user approved the design on 2026-11-12: sections 2–4, including the conservative schema-path
subset and the property boundary, with audit markers deferred. Decision signing, stored-decision
compatibility and audit payloads therefore remain outside M14. Submit the implementation plan
for the separate Phase 3 approval before writing code.

## Appendix A: Phase 1 findings

- `src/gawe/core/policy/rules.py`: `parse_rules` validates YAML, operands, rule ids and roles;
  `load_rules` only reads text. Neither has manifests. `lookup` walks mappings only.
  Negative operators match missing values; `exists` accepts variables and can resolve false.
- `src/gawe/api/wiring.py`: `assemble` registers manifests and checks implementations before
  constructing the policy engine, so validation can use the actual registry at that boundary.
- `src/gawe/core/registry.py`: registration validates JSON Schema syntax and object roots;
  `list` exposes latest versions. Registry validation does not prove policy paths exist.
- `src/gawe/core/policy/engine.py`: RBAC and schema validation precede rule matching. Evaluation
  errors deny; strictest outcome wins. Baseline tool-only allows rely on stronger matching rules.
- Both shipped app policies use only tool/metadata predicates in allow rules. Negative domain
  checks are approval/deny rules. Contract Review's two low-risk rules use runtime lists, which
  legitimately become empty. No policy content change is needed for the proposed restrictions.
- File-loader callers exist in wiring, flow/adversarial/golden helpers, app unit tests and benchmark
  tests; they must all pass the corresponding manifests. Parser-only tests remain useful.
- The example fixture uses different tool names/schemas from Contract Review; use `tests.helpers.TOOLS`,
  not the app manifests. All app schemas use explicit properties and flat policy paths today.
- Hypothesis is not a current dev dependency. Existing tests are examples/parameterizations.
- `src/gawe/core/policy/signing.py` signs a fixed v1 body. `PolicyDecision` rejects unknown fields.
  `src/gawe/engine/orchestrator.py` persists decisions and writes selected metadata to audit.
- Existing `docs/m13-design.md` and `docs/m13-plan.md` establish the document and review format.
  Their broader old milestone scope does not apply here. No AGENTS.md was found in repository
  ancestors or the searched repository files. The starting tree was clean on main; no remote is
  configured. Created `m14-policy-load-checks` without a pull.

## Appendix B: Related limits, not fixes in M14

- Explicit path existence does not prove operator/type compatibility, runtime variable completeness,
  or that a valid-looking value is true. Those require separate contracts.
- The current general-purpose JSON Schema registry supports more schemas than the proposed static
  path walker can prove; unsupported traversed structures will fail explicitly.
- Malformed address-like strings in tools with no domain rule can still pass a plain string schema.
- Optional fields and unconditional allows prevent a universal unreadability claim for every future
  policy; the shipped-policy property test guards the stated current boundary.

## Build record

S1 completed 2026-11-12. File loading now requires manifests; assembly supplies every registered
version after the review correction. All file-loader callers were updated, including `scripts/live_smoke.py` found
in the repository-wide search. Reference-validation tests live together in the new
`tests/unit/test_policy_references.py` rather than extending the operator-focused rules test file.
No shipped YAML policies or matching semantics changed.

Verification: `make check` passed (1115 passed, 39 skipped; rules.py 100% statement coverage),
`make check-docs` checked 65 references with zero problems, and `git diff --check` passed.

S1 review correction authorized 2026-11-12: `_materialize` pins the latest version, while resumed
steps evaluate their stored version. Review reproduced an older schema bypassing both low-risk
approval rules. Assembly now checks all bundle manifests; the regression rejects drift in either
the old or latest version and accepts multiple compatible versions. The earlier latest-only
restriction is removed. No runtime matching, version-selection or signing behavior changes.

S2 completed 2026-11-12. Rule validation now rejects negative allow predicates and existence
checks that can match absence, using an explicit safe operator set. Direct construction and
YAML parsing share the restriction. The existing operator tests now use deny rules so they
continue testing negative matching semantics without constructing invalid allow rules.
Approval and deny semantics and shipped policies remain unchanged. Added 65 regression cases,
including dynamic operands, guarded negatives and a newly registered unclassified operator.
Verification: `make check` passed (1181 passed, 39 skipped in 49.55s; ruff and mypy passed),
`make check-docs` checked 65 references with zero problems. No design departures in S2.

S3 completed 2026-11-12. Added Hypothesis as a dev dependency (validated with 6.170.15).
`tests/adversarial/test_policy_unreadable.py` contains 81 explicit property targets, 11 readable
controls and one coverage guard: 93 cases total. The property uses up to 40 deterministic examples
per target with no example database. Every shipped tool field is targeted for missing, null and
wrong-type input; domain checks also get schema-valid malformed addresses, and each referenced
variable gets missing/wrong-type tests. Runtime list operands vary between populated and empty.
Expected malformed-domain outcomes are independent labels, never production parser output.
Schema-invalid assertions name `engine.invalid_args`, variable failures name `engine.error`, and
all decisions verify under the signer. The coverage guard fails when a shipped app, tool, property,
policy path, domain check or variable needs a new target. Future nested runtime paths require
an explicit strategy; current shipped paths are flat. This remains bounded generated coverage,
not a proof over every possible input or future policy.

Added threat-model control T20 with seven existing test references. Final checks:

```text
ruff check src tests bench
All checks passed!
mypy
Success: no issues found in 97 source files
pytest --cov=gawe --cov-report=term-missing
1274 passed, 39 skipped in 55.96s
python scripts/check_threat_model.py
checked 72 references, 0 problem(s)
```

S3 review follow-up, 2026-11-12. Review found the shipped-policy property reached rule matching
only for malformed addresses and variables: missing, null and wrong-type inputs stop at the schema
gate. Swapping `ne` or `not_in` to fail on a missing value, or switching off the allow-rule check,
left the property green. Added a rule layer to `tests/adversarial/test_policy_unreadable.py` that
removes the schema gate: every operator, against a hand-written table, holds (negative) or fails
(positive) on unreadable values, and the table must cover every registered operator; any
single-predicate allow rule that loads, flat or nested, literal or variable operand, never allows
unreadable input through the engine; and every shipped predicate, with populated and emptied
variables, fails toward restriction. Each of these five mutations now fails the file: `ne`,
`not_in` or `domain_not_in` false on unreadable, `in` true on missing, allow-rule check off.
No production code changed.

`git diff --check` passed. Audit markers remain deferred. No roadmap or public-repo changes,
model calls, or agent-created commits. The final S3 diff is left for the user's review and commit.
