-- Checks for migration 0003. Runs after the earlier files against the same database.
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

INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-0000000000d1', 'tenant-d');
INSERT INTO users (id, tenant_id, email) VALUES
    ('00000000-0000-0000-0000-000000000021', '00000000-0000-0000-0000-0000000000d1', 'u1@d.test');
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile, document) VALUES
    ('00000000-0000-0000-0000-000000000d01', '00000000-0000-0000-0000-0000000000d1',
     '00000000-0000-0000-0000-000000000021', 'o', 'p', 'contract text');

SELECT pg_temp.expect_ok(
    $$SELECT 1 FROM runs WHERE id = '00000000-0000-0000-0000-000000000d01'
        AND roles = '{}' AND decisions = '{}'::jsonb AND error IS NULL$$,
    'new run columns have safe defaults');

SELECT pg_temp.expect_error(
    $$UPDATE runs SET findings = '{"a":1}'::jsonb WHERE id = '00000000-0000-0000-0000-000000000d01'$$,
    '23514', 'findings without a schema id are rejected');

SELECT pg_temp.expect_ok(
    $$UPDATE runs SET findings = '{"a":1}'::jsonb, findings_schema_id = 's1'
       WHERE id = '00000000-0000-0000-0000-000000000d01'$$,
    'findings with a schema id are accepted');

SELECT pg_temp.expect_error(
    $$UPDATE runs SET decisions = '[]'::jsonb WHERE id = '00000000-0000-0000-0000-000000000d01'$$,
    '23514', 'decisions must be an object');

SELECT pg_temp.expect_error(
    $$UPDATE runs SET status = 'running' WHERE id = '00000000-0000-0000-0000-000000000d01'$$,
    '23514', 'a run cannot leave planning while still holding the raw document');

SELECT pg_temp.expect_ok(
    $$UPDATE runs SET document = NULL, status = 'running'
       WHERE id = '00000000-0000-0000-0000-000000000d01'$$,
    'dropping the document allows the run to proceed');
