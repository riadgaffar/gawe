-- Checks for migration 0002. Runs after test_schema.sql against the same database, so it seeds
-- its own rows under distinct ids.
\set ON_ERROR_STOP on

CREATE FUNCTION pg_temp.expect_error(stmt text, expected text, label text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        EXECUTE stmt;
    EXCEPTION WHEN OTHERS THEN
        IF SQLSTATE = expected THEN
            RAISE NOTICE 'PASS: %', label;
            RETURN;
        END IF;
        RAISE EXCEPTION 'FAIL: % (expected %, got %: %)', label, expected, SQLSTATE, SQLERRM;
    END;
    RAISE EXCEPTION 'FAIL: % (expected error %, but the statement succeeded)', label, expected;
END $$;

CREATE FUNCTION pg_temp.expect_ok(stmt text, label text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE stmt;
    RAISE NOTICE 'PASS: %', label;
EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'FAIL: % (unexpected error %: %)', label, SQLSTATE, SQLERRM;
END $$;

INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-0000000000c1', 'tenant-c');
INSERT INTO users (id, tenant_id, email) VALUES
    ('00000000-0000-0000-0000-000000000011', '00000000-0000-0000-0000-0000000000c1', 'u1@c.test'),
    ('00000000-0000-0000-0000-000000000012', '00000000-0000-0000-0000-0000000000c1', 'u2@c.test');
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile) VALUES
    ('00000000-0000-0000-0000-000000000c01', '00000000-0000-0000-0000-0000000000c1',
     '00000000-0000-0000-0000-000000000011', 'o', 'p');
INSERT INTO plans (id, run_id, version, steps, planner_model) VALUES
    ('00000000-0000-0000-0000-000000000c0a', '00000000-0000-0000-0000-000000000c01', 1, '[]', 'fake');
INSERT INTO steps (id, run_id, plan_id, step_key, tool_name, tool_version, args_template) VALUES
    ('00000000-0000-0000-0000-000000000c11', '00000000-0000-0000-0000-000000000c01',
     '00000000-0000-0000-0000-000000000c0a', 's1', 't', '1.0.0', '{}');

SELECT pg_temp.expect_ok(
    $$UPDATE steps SET error = 'verification_failed' WHERE id = '00000000-0000-0000-0000-000000000c11'$$,
    'steps.error accepts a code');

SELECT pg_temp.expect_error(
    $$INSERT INTO approvals (step_id, action_hash, requested_by, expires_at)
      VALUES ('00000000-0000-0000-0000-000000000c11', repeat('a', 64),
              '00000000-0000-0000-0000-000000000011', now() + interval '1 hour')$$,
    '23502', 'approvals require an approver_role');

SELECT pg_temp.expect_error(
    $$INSERT INTO approvals (step_id, action_hash, requested_by, expires_at, approver_role)
      VALUES ('00000000-0000-0000-0000-000000000c11', repeat('a', 64),
              '00000000-0000-0000-0000-000000000011', now() + interval '1 hour', '')$$,
    '23514', 'approver_role cannot be empty');

SELECT pg_temp.expect_ok(
    $$INSERT INTO approvals (step_id, action_hash, requested_by, expires_at, approver_role)
      VALUES ('00000000-0000-0000-0000-000000000c11', repeat('a', 64),
              '00000000-0000-0000-0000-000000000011', now() + interval '1 hour', 'legal_reviewer')$$,
    'approval with a role is accepted');

SELECT pg_temp.expect_error(
    $$INSERT INTO executions (step_id, attempt, idempotency_key, status, finished_at, output_hash)
      VALUES ('00000000-0000-0000-0000-000000000c11', 1, 'k', 'succeeded', now(), 'nothex')$$,
    '23514', 'executions.output_hash must be hex');

SELECT pg_temp.expect_ok(
    $$INSERT INTO executions (step_id, attempt, idempotency_key, status, finished_at, output_hash)
      VALUES ('00000000-0000-0000-0000-000000000c11', 1, 'k', 'succeeded', now(), repeat('b', 64))$$,
    'executions.output_hash accepts a sha256');

SELECT pg_temp.expect_ok(
    $$INSERT INTO step_outputs (step_id, output, output_hash)
      VALUES ('00000000-0000-0000-0000-000000000c11', '{"a":1}', repeat('c', 64))$$,
    'step output can be stored');

SELECT pg_temp.expect_error(
    $$INSERT INTO step_outputs (step_id, output, output_hash)
      VALUES ('00000000-0000-0000-0000-000000000c11', '{"a":2}', repeat('c', 64))$$,
    '23505', 'one output per step');

SELECT pg_temp.expect_error(
    $$UPDATE step_outputs SET output = '{"a":2}'$$,
    '42501', 'stored output cannot be updated');

SELECT pg_temp.expect_error(
    $$INSERT INTO step_outputs (step_id, output, output_hash)
      VALUES ('00000000-0000-0000-0000-000000000c99', '{}', repeat('c', 64))$$,
    '23503', 'output must belong to a real step');
