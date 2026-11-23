-- M20: hand-off states for prepare-only actions.
\set ON_ERROR_STOP on
BEGIN;
CREATE FUNCTION pg_temp.ho_expect_error(stmt text, expected text, label text) RETURNS void
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
INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-0000000000e1', 'handoff-tenant');
INSERT INTO users (id, tenant_id, email) VALUES ('00000000-0000-0000-0000-0000000000e2',
    '00000000-0000-0000-0000-0000000000e1', 'handoff@synthetic.test');
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile, status) VALUES
    ('00000000-0000-0000-0000-0000000000e3', '00000000-0000-0000-0000-0000000000e1',
     '00000000-0000-0000-0000-0000000000e2', 'o', 'p', 'waiting_for_person');
DO $$ BEGIN RAISE NOTICE 'PASS: a run can wait for a person'; END $$;
INSERT INTO plans (id, run_id, version, steps, planner_model) VALUES
    ('00000000-0000-0000-0000-0000000000e4', '00000000-0000-0000-0000-0000000000e3', 1, '[]', 'fake');
INSERT INTO steps (run_id, plan_id, step_key, tool_name, tool_version, args_template, status,
                   action_hash, handoff)
SELECT '00000000-0000-0000-0000-0000000000e3', '00000000-0000-0000-0000-0000000000e4',
       state, 'tool', '1.0.0', '{}', state, repeat('a',64), '{"role": "legal_reviewer"}'
FROM unnest(ARRAY['prepared','done_by_person','not_done_by_person']) state;
DO $$ BEGIN RAISE NOTICE 'PASS: the three hand-off states hold a bound action and a hand-off'; END $$;
SELECT pg_temp.ho_expect_error(
    $$UPDATE steps SET handoff=NULL WHERE status='prepared'$$,
    '23514', 'a hand-off state cannot lose its hand-off record');
SELECT pg_temp.ho_expect_error(
    $$UPDATE steps SET action_hash=NULL WHERE status='prepared'$$,
    '23514', 'a prepared step cannot lose its action hash');
SELECT pg_temp.ho_expect_error(
    $$INSERT INTO steps (run_id, plan_id, step_key, tool_name, tool_version, args_template,
                         status, action_hash, handoff)
      VALUES ('00000000-0000-0000-0000-0000000000e3', '00000000-0000-0000-0000-0000000000e4',
              'other', 'tool', '1.0.0', '{}', 'succeeded', repeat('a',64), '{"role": "x"}')$$,
    '23514', 'only hand-off states (or a cancelled one) carry a hand-off record');
SELECT pg_temp.ho_expect_error(
    $$UPDATE steps SET handoff='[]' WHERE status='done_by_person'$$,
    '23514', 'a hand-off record is an object');
UPDATE steps SET status='failed' WHERE status='prepared';
DO $$ BEGIN RAISE NOTICE 'PASS: a cancelled hand-off keeps its record when it fails'; END $$;
ROLLBACK;
