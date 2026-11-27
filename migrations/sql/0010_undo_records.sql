-- M26: an undo record written before each undoable write, armed when read-back agrees.
-- Only its state, result revision, reason and revision change after it is written.
CREATE TABLE undo_records (
    undo_id text PRIMARY KEY CHECK (undo_id ~ '^[0-9a-f]{64}$'),
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 0),
    state text NOT NULL CHECK (state IN ('pending', 'armed', 'unavailable', 'requested', 'used', 'failed')),
    record jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object'),
    FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, id),
    CHECK (record ?& ARRAY['undo_id', 'tenant_id', 'run_id', 'step_id', 'action_hash',
                           'operation_key', 'bound_version', 'state', 'revision']),
    CHECK (record->>'undo_id' = undo_id),
    CHECK (record->>'tenant_id' = tenant_id::text),
    CHECK (record->>'run_id' = run_id::text),
    CHECK ((record->>'revision')::bigint = revision),
    CHECK (record->>'state' = state),
    -- Armed means read-back saw the revision after the write; unavailable says why.
    CHECK ((state IN ('armed', 'requested', 'used', 'failed')) = (record->>'result_version' IS NOT NULL)),
    CHECK ((state IN ('unavailable', 'failed')) = (record->>'reason' IS NOT NULL))
);
CREATE INDEX undo_records_run_idx ON undo_records (tenant_id, run_id);
GRANT SELECT, INSERT, UPDATE ON undo_records TO gawe_app;
