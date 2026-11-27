# M28 — Rendered approval-card tests

Status: **Done.** Design and implementation plan approved by the owner; committed by the owner and merged to `main` by fast-forward (2026-11-26).
Base: M27, branch `record-change-diff`. This is roadmap item 12.

## Approved design

Render the actual ApprovalCard and RecordEvidenceCard in a DOM with Vitest, jsdom and React
Testing Library. Keep the existing Node model tests. Replace the two source-inspection tests with
assertions on rendered output and user interaction. Keep React's useActionState/useFormStatus,
child components and links real; mock the server action so no decision reaches an API.

Check typed fields, replay tables, action hashes, earlier actions and previous-call differences.
Model prose starts collapsed, with a label and length but no preview; expanding it renders text.
Captured fields remain separate from arguments and raw snapshots. Refusal evidence distinguishes
all comparison states, guard/later timing, partial coverage, missing/null/unavailable values and
original undo provenance. Record evidence supplies no retry or approval controls.

Approve and Reject tests inspect the actual submitted form data, including the displayed hash,
approval id, decision and comment. Pending buttons disable, success ends the controls, and errors
remain visible as text with decisions available. Tests use roles, labels and scoped DOM queries.

These are component DOM tests. They do not establish browser layout, Next routing/session or
server-action transport behavior, or render the asynchronous server pages end to end. Existing
API authorization and binding tests retain that responsibility. No production behavior change is
planned; report any product defect found before expanding scope.

## Approved implementation plan

One implementation/review checkpoint:

1. Add compatible development-only dependencies and a TSX/alias/jsdom harness. Make `npm test`
   and therefore `make ui-check` run both the existing Node suite and the rendering suite.
2. Add approval rendering and decision-interaction tests with deterministic typed fixtures.
3. Add record-evidence rendering tests and replace the two source-inspection assertions.
4. Run the full checkpoint and update documentation, roadmap and project memory.

Run `make ui-check`, `make check`, `make check-docs` and the free scripted, integrity (including
undo), replay and record-changes commands. Compare all trial fields, scalar types, key sets and
array order, without exclusions. Baselines: `20261127T020458Z-scripted` (38),
`20261127T020502Z-integrity` (23), `20261127T020502Z-undo-integrity` (4),
`20261127T020505Z-replay` (9), `20261127T020505Z-record-changes` (18).
Postgres/SQL checks are required only if persistence/backend or schema changes arise.
Logs: ignored `bench-results/m28-*.txt`. Stop after two failed verification attempts. No paid
models, real connectors, staging, commits or pushes. Finish with an owner review checkpoint and
a suggested one-line commit message.

## Build record

During initial implementation, dependency setup encountered zsh interpreting unquoted
version ranges, then an npm peer conflict: latest jest-dom requires Vitest 5. The compatible
Vitest 4 / Vite 6 / jsdom 26 / jest-dom 6 lines preserve the existing Node >=22.6 floor. No forced
peer resolution or runtime dependency changes. These were setup failures before verification.


### Stopped implementation checkpoint

The harness and 21 rendered tests are implemented. `npm test` now runs the existing Node model
suite followed by Vitest; `make ui-check` therefore requires both. The two source-inspection
tests were removed in favor of rendered assertions. ApprovalCard, RecordEvidenceCard, React
form hooks, child components and Next Link remain real; only the server action is mocked.
No production component, policy, engine, API or database source was changed.

Verification attempts:

| Check | Result | Log |
|---|---|---|
| Dependency install | Compatible packages installed; npm reported 0 vulnerabilities | `m28-install-2.txt` |
| Failure 1: initial TypeScript check | Five role queries used unsupported `exact`; corrected by removing the option because string role names already match exactly | `m28-type-1.txt` |
| Corrected TypeScript check | Passed | `m28-type-2.txt` |
| Failure 2: first DOM suite | 19 passed, 2 failed; Approve and Reject cases timed out in a hook at line 10 | `m28-render-1.txt` |

The suspect is `beforeEach(() => vi.mocked(decide).mockReset())` in
`ui/tests/approval-card.render.test.tsx`. Its expression body returns the mock function.
Vitest can treat a returned function as hook cleanup; the decision tests subsequently give that
same mock an unresolved-promise implementation. An authorized continuation should make this
hook explicitly return void, then rerun type/render checks and the full checkpoint. This is a
static diagnosis, not yet confirmed by a passing rerun. Do not increase the timeout to hide it.

Stopped as required after the second failed verification attempt. No further implementation
edits or verification runs followed; only this record and project memory were updated. The full
Node suite, production build, `make check`, documentation checks and benchmark comparisons have
not run for M28. Final roadmap/threat-model/testing documentation updates remain outstanding.
The lockfile additions also need final review. No paid model calls, real connectors, staging,
commits or pushes. Changes remain uncommitted for owner review/continuation.
Suggested checkpoint commit: `wip(ui): add M28 rendering tests; two hook timeouts remain`.


### Authorized continuation from the stopped checkpoint

The owner committed the stopped checkpoint and authorized completion. The setup hook now uses a
block body and returns void. This confirms the timeout diagnosis: all 21 rendered tests pass,
including the two decision tests, without changing the timeout or replacing React's form hooks.
TypeScript passes. This continuation changes only the hook and documentation; no production
component or backend behavior changed. UI testing instructions, architecture, demo, threat-model
references and roadmap now describe the DOM coverage and its limits.

The full checkpoint passed on 2026-11-26 (America/New_York; artifact names use UTC), with zero failed verification attempts in this continuation.


Final checks in the continuation:

| Check | Result | Log |
|---|---|---|
| `make check` | Lint clean; mypy 163 files; 3,524 passed, 100 skipped, 553.07 seconds; 92% coverage | `m28-resume-check.txt` |
| Focused TypeScript and DOM tests | TypeScript clean; 21/21 rendered tests passed | `m28-resume-type.txt`, `m28-resume-render.txt` |
| `make ui-check` | TypeScript clean; 72 Node model tests and 21 DOM tests passed; production build passed | `m28-resume-ui-check.txt` |
| `make check-docs` | 285 references, 0 problems | `m28-resume-check-docs.txt` |
| Scripted benchmark | 38 records identical to baseline | `m28-resume-scripted.txt` |
| Integrity and undo benchmarks | 23 and 4 records identical to their baselines | `m28-resume-integrity.txt` |
| Replay benchmark | 9 records identical to baseline | `m28-resume-replay.txt` |
| Record-change benchmark | 18 records identical to baseline | `m28-resume-record-changes.txt` |
| Dependency review | All 54 existing runtime lockfile entries retain their versions; runtime declarations and Node minimum unchanged | `m28-resume-dependency-review.txt` |

`m28-resume-baseline-comparison.txt` compares every trial field, scalar type, key set and array
order with no exclusions. Artifacts: `20261127T023048Z-scripted`, `20261127T023051Z-integrity`,
`20261127T023051Z-undo-integrity`, `20261127T023054Z-replay`, and
`20261127T023054Z-record-changes`. The 72 model tests plus 21 DOM tests replace the previous
74-test UI suite: two source inspections removed, 21 rendered tests added. No backend,
persistence or schema changes require separate Postgres or SQL checks for this milestone.


All source and test changes were present for the full checks. Only documentation and project
memory changed afterwards; final documentation validation and `git diff --check` passed.
M28 / roadmap item 12 is complete. Browser layout, asynchronous page integration, Next session
and transport behavior, and effects on human approvers remain outside the component tests.
Item 13 still waits for a real mail system and directory. No paid model calls, real connectors,
staging, commits or pushes. Stop for owner review/commit; no next milestone has been started.
Suggested commit: `test(ui): complete M28 rendered approval coverage`.
