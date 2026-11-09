-- 0003: what the orchestrator needs to continue a run after a restart.
--   runs.roles                the acting user's roles when the run started
--   runs.document             the uploaded text, only until the Reader has read it (then NULL)
--   runs.findings(_schema_id) the Reader's validated output, all the Planner ever sees
--   runs.decisions            signed policy decisions by step key (the executor needs the exact
--                             decision a human approved)
--   runs.error                short failure code, never tool output
BEGIN;

ALTER TABLE runs ADD COLUMN roles text[] NOT NULL DEFAULT '{}';
ALTER TABLE runs ADD COLUMN document text;
ALTER TABLE runs ADD COLUMN findings_schema_id text;
ALTER TABLE runs ADD COLUMN findings jsonb;
ALTER TABLE runs ADD COLUMN decisions jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE runs ADD COLUMN error text;

ALTER TABLE runs ADD CONSTRAINT runs_findings_together
    CHECK ((findings IS NULL) = (findings_schema_id IS NULL));
ALTER TABLE runs ADD CONSTRAINT runs_decisions_object
    CHECK (jsonb_typeof(decisions) = 'object');
-- A finished run keeps no raw document.
ALTER TABLE runs ADD CONSTRAINT runs_no_document_when_finished
    CHECK (document IS NULL OR status IN ('created', 'planning'));

CREATE INDEX runs_tenant_created_idx ON runs (tenant_id, created_at DESC);

COMMIT;
