-- Next-day review (M16b): tenant-scoped review jobs, review items, and an index for the daily
-- scan of policy decisions in the audit log.

-- A daily_review job belongs to a tenant, not a run: its run fields are null. Every other kind
-- still names its run, and the stored record must agree with the indexed column either way.
ALTER TABLE maintenance_jobs ALTER COLUMN run_id DROP NOT NULL;
DO $$
DECLARE c text;
BEGIN
    SELECT conname INTO STRICT c FROM pg_constraint
    WHERE conrelid = 'maintenance_jobs'::regclass AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%= (run_id)::text%';
    EXECUTE format('ALTER TABLE maintenance_jobs DROP CONSTRAINT %I', c);
END $$;
-- COALESCE: a comparison with a null is unknown, which a CHECK would let through.
ALTER TABLE maintenance_jobs ADD CONSTRAINT maintenance_jobs_run_record CHECK (
    CASE WHEN run_id IS NULL THEN COALESCE(record->'run_id' = 'null'::jsonb, false)
         ELSE COALESCE(record->>'run_id' = run_id::text, false) END
);
ALTER TABLE maintenance_jobs ADD CONSTRAINT maintenance_jobs_tenant_scope CHECK (
    (run_id IS NULL) = (record->>'kind' IS NOT DISTINCT FROM 'daily_review')
);

-- What the review found or sampled, and a reviewer's verdict on it.
CREATE TABLE review_items (
    item_id     text PRIMARY KEY CHECK (item_id ~ '^[0-9a-f]{64}$'),
    tenant_id   uuid NOT NULL REFERENCES tenants(id),
    day         date NOT NULL,
    run_id      uuid NOT NULL,
    step_key    text,
    action_hash text CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    reason      text NOT NULL CHECK (reason IN (
        'audit_mismatch', 'superseded', 'signature_invalid', 'action_mismatch',
        'rules_changed', 'rules_may_have_changed', 'inputs_changed', 'sampled_low')),
    key_id      text CHECK (key_id ~ '^[0-9a-f]{16}$'),
    status      text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'confirmed', 'missed_risk')),
    created_at  timestamptz NOT NULL,
    event_seq   integer CHECK (event_seq >= 1),
    decided_by  uuid REFERENCES users(id),
    decided_at  timestamptz,
    FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, id),
    -- a sample always names the key it was drawn with; nothing else does
    CHECK ((reason = 'sampled_low') = (key_id IS NOT NULL)),
    -- a verdict, its decider and its time are set together, once
    CHECK ((status = 'open') = (decided_by IS NULL)),
    CHECK ((decided_by IS NULL) = (decided_at IS NULL))
);
CREATE INDEX review_items_tenant_status_idx ON review_items (tenant_id, status, day);

CREATE INDEX audit_events_type_ts_idx ON audit_events (type, ts);

GRANT SELECT, INSERT ON review_items TO gawe_app;
GRANT UPDATE (status, decided_by, decided_at, event_seq) ON review_items TO gawe_app;
