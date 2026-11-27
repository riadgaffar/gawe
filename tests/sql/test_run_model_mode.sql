-- Migration 0012: a run may record how the scripted demo model behaved for it, or nothing.
\set ON_ERROR_STOP on
BEGIN;
CREATE FUNCTION pg_temp.mm_expect_error(stmt text, expected text, label text) RETURNS void
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
INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-0000000000e1', 'mode-tenant');
INSERT INTO users (id, tenant_id, email) VALUES
    ('00000000-0000-0000-0000-0000000000e2', '00000000-0000-0000-0000-0000000000e1', 'op@synthetic.test');
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile) VALUES
    ('00000000-0000-0000-0000-0000000000e3', '00000000-0000-0000-0000-0000000000e1',
     '00000000-0000-0000-0000-0000000000e2', 'o', 'contract_review');
DO $$ BEGIN
    IF (SELECT model_mode FROM runs WHERE id = '00000000-0000-0000-0000-0000000000e3') IS NOT NULL THEN
        RAISE EXCEPTION 'FAIL: a run without a mode reads back with one';
    END IF;
    RAISE NOTICE 'PASS: a run made without a mode has none';
END $$;
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile, model_mode) VALUES
    ('00000000-0000-0000-0000-0000000000e4', '00000000-0000-0000-0000-0000000000e1',
     '00000000-0000-0000-0000-0000000000e2', 'o', 'contract_review', 'honest'),
    ('00000000-0000-0000-0000-0000000000e5', '00000000-0000-0000-0000-0000000000e1',
     '00000000-0000-0000-0000-0000000000e2', 'o', 'contract_review', 'fooled');
DO $$ BEGIN RAISE NOTICE 'PASS: honest and fooled are accepted'; END $$;
SELECT pg_temp.mm_expect_error(
    $$UPDATE runs SET model_mode = 'real' WHERE id = '00000000-0000-0000-0000-0000000000e3'$$,
    '23514', 'any other mode is refused');
SET LOCAL ROLE gawe_app;
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile, model_mode) VALUES
    ('00000000-0000-0000-0000-0000000000e6', '00000000-0000-0000-0000-0000000000e1',
     '00000000-0000-0000-0000-0000000000e2', 'o', 'contract_review', 'fooled');
SELECT model_mode FROM runs WHERE id = '00000000-0000-0000-0000-0000000000e6';
RESET ROLE;
DO $$ BEGIN RAISE NOTICE 'PASS: the app role can write and read a run''s mode'; END $$;
ROLLBACK;
