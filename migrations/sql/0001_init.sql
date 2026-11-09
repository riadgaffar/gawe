-- GAWE initial schema. Plain SQL so it can be verified with psql and wrapped by Alembic.
-- Requires PostgreSQL 13+ (gen_random_uuid() is built in).
--
-- Deployment note: run the application as a NON-owner role granted only SELECT/INSERT on
-- audit_events (and the normal DML on other tables). The triggers below make audit_events
-- append-only even for the table owner, but an owner or superuser can still drop them.

BEGIN;

-- ------------------------------------------------------------------ identity

CREATE TABLE tenants (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id),
    email        text NOT NULL,
    display_name text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email),
    UNIQUE (tenant_id, id)
);

CREATE TABLE roles (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    name      text NOT NULL,
    UNIQUE (tenant_id, name)
);

CREATE TABLE user_roles (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id uuid NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);

-- ---------------------------------------------------------------------- tools

CREATE TABLE tools (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenants(id),
    name              text NOT NULL,
    version           text NOT NULL CHECK (version ~ '^\d+\.\d+\.\d+$'),
    manifest          jsonb NOT NULL,
    side_effect_class text NOT NULL CHECK (side_effect_class IN ('read', 'write', 'external')),
    risk_tier         text NOT NULL CHECK (risk_tier IN ('low', 'medium', 'high')),
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name, version)
);

-- ------------------------------------------------------------------ artifacts

CREATE TABLE artifacts (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id),
    sha256       text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    storage_uri  text NOT NULL,
    content_type text NOT NULL,
    size_bytes   bigint NOT NULL CHECK (size_bytes >= 0),
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, sha256)
);

-- ------------------------------------------------------------------- runs/plans

CREATE TABLE runs (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES tenants(id),
    acting_user_id     uuid NOT NULL,
    objective          text NOT NULL,
    profile            text NOT NULL,
    status             text NOT NULL DEFAULT 'created' CHECK (status IN (
        'created', 'planning', 'running', 'paused_for_approval',
        'completed', 'failed', 'cancelled', 'denied')),
    current_plan_version integer NOT NULL DEFAULT 0 CHECK (current_plan_version >= 0),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    completed_at       timestamptz,
    -- the acting user must belong to the same tenant as the run
    FOREIGN KEY (tenant_id, acting_user_id) REFERENCES users (tenant_id, id)
);
CREATE INDEX runs_tenant_status_idx ON runs (tenant_id, status);

CREATE TABLE plans (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id       uuid NOT NULL REFERENCES runs(id),
    version      integer NOT NULL CHECK (version >= 1),
    steps        jsonb NOT NULL,
    planner_model text NOT NULL,
    validated_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, version)
);

CREATE TABLE steps (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        uuid NOT NULL REFERENCES runs(id),
    plan_id       uuid NOT NULL REFERENCES plans(id),
    step_key      text NOT NULL,
    tool_name     text NOT NULL,
    tool_version  text NOT NULL,
    args_template jsonb NOT NULL,
    resolved_args jsonb,
    action_hash   text CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    depends_on    text[] NOT NULL DEFAULT '{}',
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'ready', 'policy_checked', 'awaiting_approval', 'approved',
        'rejected', 'denied', 'executing', 'verifying', 'succeeded', 'failed')),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (plan_id, step_key),
    -- a step that is past policy must carry the exact hash it was judged on; 'failed' is exempt
    -- because a step can fail (e.g. an upstream step failed) before it is ever evaluated
    CHECK (status IN ('pending', 'ready', 'failed') OR action_hash IS NOT NULL)
);
CREATE INDEX steps_run_idx ON steps (run_id, status);

-- ---------------------------------------------------------- decisions/approvals

CREATE TABLE policy_decisions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    step_id       uuid NOT NULL REFERENCES steps(id),
    action_hash   text NOT NULL CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    outcome       text NOT NULL CHECK (outcome IN ('allow', 'needs_approval', 'deny')),
    rule_ids      text[] NOT NULL,
    approver_role text,
    reason        text NOT NULL,
    signature     text NOT NULL CHECK (signature ~ '^[0-9a-f]{64}$'),
    evaluated_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((outcome = 'needs_approval') = (approver_role IS NOT NULL))
);
CREATE INDEX policy_decisions_step_idx ON policy_decisions (step_id);

CREATE TABLE approvals (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    step_id      uuid NOT NULL REFERENCES steps(id),
    action_hash  text NOT NULL CHECK (action_hash ~ '^[0-9a-f]{64}$'),
    requested_by uuid NOT NULL REFERENCES users(id),
    requested_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    decided_by   uuid REFERENCES users(id),
    decision     text CHECK (decision IN ('approved', 'rejected')),
    comment      text,
    decided_at   timestamptz,
    CHECK (expires_at > requested_at),
    -- decision, decider and time are all present or all absent
    CHECK ((decision IS NULL) = (decided_by IS NULL) AND (decision IS NULL) = (decided_at IS NULL)),
    -- separation of duties, enforced here as well as in application code
    CHECK (decided_by IS NULL OR decided_by <> requested_by)
);
-- at most one open approval per step
CREATE UNIQUE INDEX approvals_one_pending_per_step ON approvals (step_id) WHERE decision IS NULL;

-- ----------------------------------------------------------------- executions

CREATE TABLE executions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    step_id            uuid NOT NULL REFERENCES steps(id),
    attempt            integer NOT NULL CHECK (attempt >= 1),
    idempotency_key    text NOT NULL,
    status             text NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
    output_artifact_id uuid REFERENCES artifacts(id),
    error              text,
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    UNIQUE (step_id, attempt),
    CHECK ((status = 'started') = (finished_at IS NULL))
);

-- ---------------------------------------------------------------------- audit

CREATE TABLE audit_events (
    run_id       uuid NOT NULL REFERENCES runs(id),
    seq          bigint NOT NULL CHECK (seq >= 1),
    type         text NOT NULL CHECK (length(type) BETWEEN 1 AND 128),
    actor        text NOT NULL CHECK (length(actor) BETWEEN 1 AND 128),
    payload_hash text NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    prev_hash    text NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
    hash         text NOT NULL CHECK (hash ~ '^[0-9a-f]{64}$'),
    ts           timestamptz NOT NULL,
    PRIMARY KEY (run_id, seq),
    UNIQUE (hash)
);

-- Each insert must extend its run's chain: seq = last + 1 and prev_hash = last hash
-- (genesis for the first event). Hash correctness itself is checked by the application's
-- verify_chain(); the database guarantees linkage and immutability.
CREATE FUNCTION audit_events_extend_chain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    last_seq  bigint;
    last_hash text;
BEGIN
    SELECT seq, hash INTO last_seq, last_hash
      FROM audit_events WHERE run_id = NEW.run_id ORDER BY seq DESC LIMIT 1;
    IF NOT FOUND THEN
        IF NEW.seq <> 1 OR NEW.prev_hash <> repeat('0', 64) THEN
            RAISE EXCEPTION 'audit chain: first event must have seq 1 and the genesis prev_hash'
                USING ERRCODE = 'check_violation';
        END IF;
    ELSIF NEW.seq <> last_seq + 1 OR NEW.prev_hash <> last_hash THEN
        RAISE EXCEPTION 'audit chain: event does not extend the chain (expected seq %)', last_seq + 1
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER audit_events_extend_chain
    BEFORE INSERT ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_extend_chain();

CREATE FUNCTION audit_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only (% is not allowed)', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE TRIGGER audit_events_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();

CREATE TRIGGER audit_events_no_truncate
    BEFORE TRUNCATE ON audit_events
    FOR EACH STATEMENT EXECUTE FUNCTION audit_events_append_only();

-- -------------------------------------------------------------------- metrics

CREATE TABLE eval_metrics (
    id          bigserial PRIMARY KEY,
    run_id      uuid NOT NULL REFERENCES runs(id),
    name        text NOT NULL,
    value       double precision NOT NULL,
    labels      jsonb NOT NULL DEFAULT '{}',
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX eval_metrics_run_idx ON eval_metrics (run_id, name);

COMMIT;
