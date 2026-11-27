-- Migration 0010: an undo record is armed only with the revision after the write, unavailable only
-- with a reason, scoped to its run's tenant; the app role may write it but not delete it.
\set ON_ERROR_STOP on
BEGIN;
CREATE FUNCTION pg_temp.ur_expect_error(stmt text, expected text, label text) RETURNS void
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
CREATE FUNCTION pg_temp.ur_record(state text, extra jsonb) RETURNS jsonb
LANGUAGE sql AS $$
    SELECT jsonb_build_object(
        'undo_id', repeat('a', 64), 'tenant_id', '00000000-0000-0000-0000-0000000000e1',
        'run_id', '00000000-0000-0000-0000-0000000000e3', 'step_id', 'write',
        'action_hash', repeat('b', 64), 'operation_key', 'k', 'bound_version', 'v1',
        'state', state, 'revision', 0) || extra
$$;
INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-0000000000e1', 'undo-tenant');
INSERT INTO users (id, tenant_id, email) VALUES
    ('00000000-0000-0000-0000-0000000000e2', '00000000-0000-0000-0000-0000000000e1', 'a@synthetic.test');
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile) VALUES
    ('00000000-0000-0000-0000-0000000000e3', '00000000-0000-0000-0000-0000000000e1',
     '00000000-0000-0000-0000-0000000000e2', 'o', 'contract_review');
SELECT pg_temp.ur_expect_error(
    $$INSERT INTO undo_records VALUES (repeat('a', 64), '00000000-0000-0000-0000-0000000000e1',
      '00000000-0000-0000-0000-0000000000e3', 0, 'armed', pg_temp.ur_record('armed', '{}'))$$,
    '23514', 'armed needs the revision after the write');
SELECT pg_temp.ur_expect_error(
    $$INSERT INTO undo_records VALUES (repeat('a', 64), '00000000-0000-0000-0000-0000000000e1',
      '00000000-0000-0000-0000-0000000000e3', 0, 'unavailable',
      pg_temp.ur_record('unavailable', '{}'))$$,
    '23514', 'unavailable needs a reason');
SELECT pg_temp.ur_expect_error(
    $$INSERT INTO undo_records VALUES (repeat('a', 64), '00000000-0000-0000-0000-0000000000e1',
      '00000000-0000-0000-0000-0000000000e3', 0, 'pending', pg_temp.ur_record('armed',
      '{"result_version": "v2"}'))$$,
    '23514', 'the state column matches the record');
SELECT pg_temp.ur_expect_error(
    $$INSERT INTO undo_records VALUES (repeat('a', 64), '00000000-0000-0000-0000-0000000000e1',
      '00000000-0000-0000-0000-0000000000f9', 0, 'pending', pg_temp.ur_record('pending',
      '{"run_id": "00000000-0000-0000-0000-0000000000f9"}'))$$,
    '23503', 'a record belongs to a run of its tenant');
SET LOCAL ROLE gawe_app;
INSERT INTO undo_records VALUES (repeat('a', 64), '00000000-0000-0000-0000-0000000000e1',
    '00000000-0000-0000-0000-0000000000e3', 0, 'pending', pg_temp.ur_record('pending', '{}'));
UPDATE undo_records SET revision = 1, state = 'armed',
    record = pg_temp.ur_record('armed', '{"result_version": "v2", "revision": 1}');
DO $$ BEGIN RAISE NOTICE 'PASS: the app role writes and arms a record'; END $$;
SELECT pg_temp.ur_expect_error($$DELETE FROM undo_records$$, '42501',
    'the app role cannot delete a record');
RESET ROLE;
-- Reconstruct S2's original constraints and upgrade in place, retaining its armed row.
ALTER TABLE undo_records DROP CONSTRAINT undo_state_valid,
    DROP CONSTRAINT undo_result_required, DROP CONSTRAINT undo_reason_required;
ALTER TABLE undo_records
    ADD CHECK (state IN ('pending', 'armed', 'unavailable')),
    ADD CHECK ((state = 'armed') = (record->>'result_version' IS NOT NULL)),
    ADD CHECK ((state = 'unavailable') = (record->>'reason' IS NOT NULL));
\i migrations/sql/0011_undo_reversal_states.sql
DO $$ BEGIN
    IF (SELECT count(*) FROM undo_records WHERE state = 'armed') <> 1 THEN
        RAISE EXCEPTION 'FAIL: upgrade lost the armed record';
    END IF;
    RAISE NOTICE 'PASS: upgrade retains an existing S2 record';
END $$;
SET LOCAL ROLE gawe_app;
UPDATE undo_records SET state='requested', record=record || '{"state":"requested"}';
UPDATE undo_records SET state='used', record=record || '{"state":"used"}';
DO $$ BEGIN RAISE NOTICE 'PASS: requested and used retain the result revision'; END $$;
SELECT pg_temp.ur_expect_error(
    $$UPDATE undo_records SET state='failed', record=record || '{"state":"failed"}'$$,
    '23514', 'failed needs a reason');
UPDATE undo_records SET state='failed',
    record=record || '{"state":"failed", "reason":"record_changed"}';
DO $$ BEGIN RAISE NOTICE 'PASS: failed retains its reason and result revision'; END $$;
RESET ROLE;
ROLLBACK;
