# M27 — Approved build plan

The owner approved the design and this plan in chat, then authorized implementation. Work starts
from M26 on `record-change-diff`. See [the design](m27-design.md) for the semantics.
Each step ends at an owner review/commit checkpoint. Never stage, commit, or push.

## S1 — Snapshot and diagnostic contracts

Add bounded typed field declarations, revision-bound snapshots, refusal observations and
comparison states. Preserve missing/null and unavailable/unchanged distinctions, observation
timing, and multi-target coverage. Validate action and target bindings. Use current JSON storage.
Add trusted plan/request version pins with fixed legacy semantics; keep pins out of model plans.
Test shape rejection, hash tampering, storage round-trips, old plans and approvals, and exact
trusted version selection across registry upgrades. Do not activate new app schemas yet.

## S2 — Capture at preparation and use the captured policy values

Declare each app's tracked fields and activate versioned preparation. Capture selected fields
and guarded revisions together. Derive resource policy inputs from that same capture, including
contract deal value. IAM explicitly tracks no account profile fields. Preserve existing mismatch
refusals. Test consistency, hash binding, and compatibility of previously materialized steps.

## S3 — Persist refusal observations

Record guard evidence when available and a clearly labeled later diagnostic read otherwise.
Handle unreadable/missing targets, incomplete batches, and crash recovery. Persist evidence so
refresh does not silently replace it with a newer state. Prove zero additional effects and no
retry, reapproval, or change to the refusal outcome.

## S4 — Undo and historical compatibility

Carry trustworthy original captured evidence into undo, with provenance. Preserve old requests,
plans, hashes, approvals, and snapshotless undo behavior. Keep current-state daily review
semantics. Exercise recovery and mixed-version cases.

## S5 — API and approval/failure UI

Expose safe before/observed rows and observation timing. Show snapshots at approval and the
explicit comparison states on refusal, including no fields tracked and unavailable evidence.
Test API/card models and run the UI suite/build. Item 12's rendered-page tests remain separate.

## S6 — Adversarial benchmark and final documentation

Add a dedicated benchmark group for record-change explanations, including tampering, missing
or partial evidence, misleading unchanged fields, later reads, old records, undo, and replay.
Finish architecture, threat model, demo, and roadmap documentation and the full checkpoint.

## Checkpoint verification

At every step run `make check`, `make check-docs`, and the free scripted, integrity (including
undo), and replay benchmarks. Compare every field, scalar type, key set and array order against
the established baselines, with no exclusions. Run Postgres integration when persistence/backend
paths are exercised, SQL schema checks for schema changes, and UI tests/build for UI changes.
Write logs under ignored `bench-results/m27-sN-*.txt`; report results and material limits in the
build record. Stop after two failed verification attempts for a step. Do not run paid models or
real connectors. Do not proceed to the next step without the owner's continuation.

M26 baselines: scripted 38 (`20261125T041836Z-scripted`), integrity 23
(`20261125T201949Z-integrity`), undo 4 (`20261125T203710Z-undo-integrity`), replay 9
(`20261125T204921Z-replay`). Python 3,347 passed / 93 skipped; Postgres 143; SQL 118;
UI 60 plus build; threat-model references 275 with zero problems.
