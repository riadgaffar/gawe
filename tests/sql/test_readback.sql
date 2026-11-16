-- S3 states, tenant identity, persisted deadline and application-role access.
\set ON_ERROR_STOP on
BEGIN;
CREATE FUNCTION pg_temp.rb_expect_error(stmt text, expected text, label text) RETURNS void
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
INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-0000000000f1', 'readback-tenant');
INSERT INTO users (id, tenant_id, email) VALUES ('00000000-0000-0000-0000-0000000000f2',
    '00000000-0000-0000-0000-0000000000f1', 'readback@synthetic.test');
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile, status) VALUES
    ('00000000-0000-0000-0000-0000000000f3', '00000000-0000-0000-0000-0000000000f1',
     '00000000-0000-0000-0000-0000000000f2', 'o', 'p', 'waiting_verification');
UPDATE runs SET status='unknown' WHERE id='00000000-0000-0000-0000-0000000000f3';
DO $$ BEGIN RAISE NOTICE 'PASS: waiting_verification and unknown are representable'; END $$;
INSERT INTO plans (id, run_id, version, steps, planner_model) VALUES
    ('00000000-0000-0000-0000-0000000000f4', '00000000-0000-0000-0000-0000000000f3', 1, '[]', 'fake');
INSERT INTO steps (run_id, plan_id, step_key, tool_name, tool_version, args_template, status, action_hash)
SELECT '00000000-0000-0000-0000-0000000000f3', '00000000-0000-0000-0000-0000000000f4',
       state, 'tool', '1.0.0', '{}', state, repeat('a',64)
FROM unnest(ARRAY['awaiting_readback','verification_blocked','verification_unknown']) state;
DO $$ BEGIN RAISE NOTICE 'PASS: all three verification step states accept bound actions'; END $$;
SELECT pg_temp.rb_expect_error(
    $$UPDATE steps SET action_hash=NULL WHERE status='awaiting_readback'$$,
    '23514', 'awaiting read-back cannot lose its action hash');
SELECT pg_temp.rb_expect_error(
    $$INSERT INTO maintenance_jobs (job_id,tenant_id,run_id,revision,due_at,record)
      VALUES (repeat('b',64),'00000000-0000-0000-0000-0000000000f1',
              '00000000-0000-0000-0000-0000000000f3',0,now(),'{}')$$,
    '23514','durable jobs require identity and original deadline metadata');
INSERT INTO maintenance_jobs (job_id,tenant_id,run_id,revision,due_at,record)
VALUES (repeat('b',64),'00000000-0000-0000-0000-0000000000f1',
        '00000000-0000-0000-0000-0000000000f3',0,now(),jsonb_build_object(
          'job_id',repeat('b',64),'tenant_id','00000000-0000-0000-0000-0000000000f1',
          'run_id','00000000-0000-0000-0000-0000000000f3','revision',0,'continued',false,
          'created_at','2026-01-01T12:00:00Z','deadline','2026-01-01T12:05:00Z'));
SELECT pg_temp.rb_expect_error(
    $$UPDATE maintenance_jobs SET record=jsonb_set(record,'{deadline}','"2026-01-01T11:00:00Z"')$$,
    '23514','deadline cannot precede dispatch intent');
SELECT pg_temp.rb_expect_error(
    $$UPDATE maintenance_jobs SET record=jsonb_set(record,'{tenant_id}','"another-tenant"')$$,
    '23514','evidence metadata cannot name a different tenant');
SELECT pg_temp.rb_expect_error(
    $$UPDATE maintenance_jobs SET revision=1$$,
    '23514','job revision metadata matches its indexed CAS revision');
SET LOCAL ROLE gawe_app;
UPDATE maintenance_jobs SET due_at=due_at;
SELECT count(*) FROM maintenance_jobs;
RESET ROLE;
DO $$ BEGIN RAISE NOTICE 'PASS: app role can read and maintain verification jobs'; END $$;
SELECT pg_temp.rb_expect_error(
    $$SET LOCAL ROLE gawe_app; DELETE FROM maintenance_jobs$$,
    '42501','application cannot delete maintenance evidence');
ROLLBACK;
