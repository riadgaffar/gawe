-- Durable dispatch intent, independent evidence and recoverable continuation.
ALTER TABLE runs DROP CONSTRAINT runs_status_check;
ALTER TABLE runs ADD CONSTRAINT runs_status_check CHECK (status IN (
    'created', 'planning', 'running', 'paused_for_approval', 'waiting_verification',
    'completed', 'failed', 'cancelled', 'denied', 'unknown'));
ALTER TABLE steps DROP CONSTRAINT steps_status_check;
ALTER TABLE steps ADD CONSTRAINT steps_status_check CHECK (status IN (
    'pending', 'ready', 'policy_checked', 'awaiting_approval', 'approved',
    'rejected', 'denied', 'executing', 'verifying', 'awaiting_readback',
    'verification_blocked', 'verification_unknown', 'succeeded', 'failed'));
ALTER TABLE runs ADD CONSTRAINT runs_tenant_id_id_key UNIQUE (tenant_id, id);
CREATE TABLE maintenance_jobs (
    job_id text PRIMARY KEY CHECK (job_id ~ '^[0-9a-f]{64}$'),
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 0),
    due_at timestamptz NOT NULL,
    lease_until timestamptz,
    continued boolean NOT NULL DEFAULT false,
    record jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object'),
    FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, id),
    CHECK (record ?& ARRAY['job_id', 'tenant_id', 'run_id', 'created_at', 'deadline',
                           'revision', 'continued']),
    CHECK (record->>'job_id' = job_id),
    CHECK (record->>'tenant_id' = tenant_id::text),
    CHECK (record->>'run_id' = run_id::text),
    CHECK ((record->>'revision')::bigint = revision),
    CHECK ((record->>'continued')::boolean = continued),
    CHECK ((record->>'deadline')::timestamptz >= (record->>'created_at')::timestamptz)
);
CREATE INDEX maintenance_jobs_due_idx ON maintenance_jobs (due_at, job_id) WHERE NOT continued;
CREATE INDEX maintenance_jobs_run_idx ON maintenance_jobs (tenant_id, run_id);
GRANT SELECT, INSERT, UPDATE ON maintenance_jobs TO gawe_app;
