-- Checks for migration 0004: what the application role can and cannot do. Runs after the earlier
-- files against the same database, as a superuser, and switches to gawe_app with SET ROLE.
\set ON_ERROR_STOP on

-- Everything below is rolled back: the audit log cannot be cleaned up afterwards, and later test
-- files count its rows.
BEGIN;

CREATE FUNCTION pg_temp.expect_ok(stmt text, label text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    EXECUTE stmt;
    RAISE NOTICE 'PASS: %', label;
EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'FAIL: % (unexpected error %: %)', label, SQLSTATE, SQLERRM;
END $$;

-- Runs one statement as gawe_app and checks the outcome: 'ok' or the expected SQLSTATE.
CREATE FUNCTION pg_temp.as_app(stmt text, expected text, label text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    SET LOCAL ROLE gawe_app;
    BEGIN
        EXECUTE stmt;
    EXCEPTION WHEN OTHERS THEN
        RESET ROLE;
        IF expected = 'ok' THEN
            RAISE EXCEPTION 'FAIL: % (unexpected error %: %)', label, SQLSTATE, SQLERRM;
        ELSIF SQLSTATE = expected THEN
            RAISE NOTICE 'PASS: %', label;
            RETURN;
        END IF;
        RAISE EXCEPTION 'FAIL: % (expected %, got %: %)', label, expected, SQLSTATE, SQLERRM;
    END;
    RESET ROLE;
    IF expected = 'ok' THEN
        RAISE NOTICE 'PASS: %', label;
    ELSE
        RAISE EXCEPTION 'FAIL: % (expected error %, but the statement succeeded)', label, expected;
    END IF;
END $$;

-- Data to work on (inserted as the owner).
INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-0000000000e1', 'tenant-e');
INSERT INTO users (id, tenant_id, email) VALUES
    ('00000000-0000-0000-0000-000000000031', '00000000-0000-0000-0000-0000000000e1', 'u@e.test');

-- The role exists, cannot log in until an operator enables it, and holds no special powers.
SELECT pg_temp.expect_ok(
    $$SELECT 1 FROM pg_roles WHERE rolname = 'gawe_app' AND NOT rolcanlogin
        AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication$$,
    'gawe_app is a plain role with no login, superuser, createdb or createrole');

-- It can do what the application does.
SELECT pg_temp.as_app(
    $$INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile)
      VALUES ('00000000-0000-0000-0000-000000000e01', '00000000-0000-0000-0000-0000000000e1',
              '00000000-0000-0000-0000-000000000031', 'o', 'p')$$,
    'ok', 'the app role can create a run');
SELECT pg_temp.as_app(
    $$UPDATE runs SET status = 'planning' WHERE id = '00000000-0000-0000-0000-000000000e01'$$,
    'ok', 'the app role can update a run');
SELECT pg_temp.as_app(
    $$SELECT count(*) FROM runs$$, 'ok', 'the app role can read runs');
SELECT pg_temp.as_app(
    $$INSERT INTO audit_events (run_id, seq, type, actor, payload_hash, prev_hash, hash, ts)
      VALUES ('00000000-0000-0000-0000-000000000e01', 1, 'run.created', 'a',
              repeat('1', 64), repeat('0', 64), repeat('2', 64), now())$$,
    'ok', 'the app role can append to the audit log');

-- It cannot rewrite or remove history, or change the schema.
SELECT pg_temp.as_app(
    $$UPDATE audit_events SET actor = 'mallory'$$, '42501',
    'the app role cannot UPDATE audit_events (permission denied)');
SELECT pg_temp.as_app(
    $$DELETE FROM audit_events$$, '42501',
    'the app role cannot DELETE from audit_events (permission denied)');
SELECT pg_temp.as_app(
    $$TRUNCATE audit_events$$, '42501',
    'the app role cannot TRUNCATE audit_events (permission denied)');
SELECT pg_temp.as_app(
    $$DELETE FROM runs$$, '42501', 'the app role cannot DELETE runs');
SELECT pg_temp.as_app(
    $$UPDATE plans SET version = version$$, '42501', 'plans are insert-only for the app role');
SELECT pg_temp.as_app(
    $$UPDATE step_outputs SET output_hash = output_hash$$, '42501',
    'step outputs are insert-only for the app role');
SELECT pg_temp.as_app(
    $$INSERT INTO tenants (id, name) VALUES (gen_random_uuid(), 'x')$$, '42501',
    'the app role cannot create tenants');
SELECT pg_temp.as_app(
    $$CREATE TABLE evil (x int)$$, '42501', 'the app role cannot create tables');
SELECT pg_temp.as_app(
    $$DROP TABLE audit_events$$, '42501', 'the app role cannot drop tables');
SELECT pg_temp.as_app(
    $$ALTER TABLE audit_events DISABLE TRIGGER audit_events_no_update_delete$$, '42501',
    'the app role cannot disable the audit triggers');

-- A table added by a later migration without a grant is caught here, not in production.
SELECT pg_temp.expect_ok(
    $$SELECT 1 WHERE NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND NOT has_table_privilege('gawe_app', c.oid, 'SELECT'))$$,
    'every table in the schema is readable by the app role (new tables need a GRANT)');
SELECT pg_temp.expect_ok(
    $$SELECT 1 WHERE NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
          AND (has_table_privilege('gawe_app', c.oid, 'DELETE')
            OR has_table_privilege('gawe_app', c.oid, 'TRUNCATE')))$$,
    'the app role has DELETE or TRUNCATE on no table');

ROLLBACK;
