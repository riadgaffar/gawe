-- Migration 0008: a run may name the record its document came from.
\set ON_ERROR_STOP on
BEGIN;
CREATE FUNCTION pg_temp.rs_expect_error(stmt text, expected text, label text) RETURNS void
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
    RAISE EXCEPTION 'FAIL: % (expected error %, statement succeeded)', label, expected;
END $$;
INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-0000000000d1', 'source-tenant');
INSERT INTO users (id, tenant_id, email) VALUES
    ('00000000-0000-0000-0000-0000000000d2', '00000000-0000-0000-0000-0000000000d1', 'agent@synthetic.test');
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile, source_id) VALUES
    ('00000000-0000-0000-0000-0000000000d3', '00000000-0000-0000-0000-0000000000d1',
     '00000000-0000-0000-0000-0000000000d2', 'o', 'it_access', 'AR-2041');
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile) VALUES
    ('00000000-0000-0000-0000-0000000000d4', '00000000-0000-0000-0000-0000000000d1',
     '00000000-0000-0000-0000-0000000000d2', 'o', 'contract_review');
DO $$ BEGIN RAISE NOTICE 'PASS: a run may name its source, or none'; END $$;
SELECT pg_temp.rs_expect_error(
    $$UPDATE runs SET source_id = '../etc/passwd' WHERE id = '00000000-0000-0000-0000-0000000000d4'$$,
    '23514', 'a malformed source id is refused');
SELECT pg_temp.rs_expect_error(
    $$UPDATE runs SET source_id = repeat('a', 65) WHERE id = '00000000-0000-0000-0000-0000000000d4'$$,
    '23514', 'a source id longer than 64 characters is refused');
SET LOCAL ROLE gawe_app;
SELECT source_id FROM runs WHERE id = '00000000-0000-0000-0000-0000000000d3';
RESET ROLE;
DO $$ BEGIN RAISE NOTICE 'PASS: the app role can read a run''s source'; END $$;
ROLLBACK;
