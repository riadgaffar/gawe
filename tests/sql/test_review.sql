-- Migration 0007: tenant-scoped review jobs, review items and least-privilege access.
\set ON_ERROR_STOP on
BEGIN;
CREATE FUNCTION pg_temp.rv_expect_error(stmt text, expected text, label text) RETURNS void
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
INSERT INTO tenants (id, name) VALUES ('00000000-0000-0000-0000-0000000000e1', 'review-tenant');
INSERT INTO users (id, tenant_id, email) VALUES
    ('00000000-0000-0000-0000-0000000000e2', '00000000-0000-0000-0000-0000000000e1', 'req@synthetic.test'),
    ('00000000-0000-0000-0000-0000000000e3', '00000000-0000-0000-0000-0000000000e1', 'rev@synthetic.test');
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile) VALUES
    ('00000000-0000-0000-0000-0000000000e4', '00000000-0000-0000-0000-0000000000e1',
     '00000000-0000-0000-0000-0000000000e2', 'o', 'p');

CREATE FUNCTION pg_temp.job(kind text, run uuid) RETURNS jsonb LANGUAGE sql AS $$
    SELECT jsonb_build_object('job_id', repeat('c', 64), 'kind', kind,
        'tenant_id', '00000000-0000-0000-0000-0000000000e1', 'run_id', run, 'revision', 0,
        'continued', false, 'created_at', '2026-11-16T00:00:00Z', 'deadline', '2026-11-17T01:00:00Z')
$$;
INSERT INTO maintenance_jobs (job_id, tenant_id, run_id, revision, due_at, record)
VALUES (repeat('c', 64), '00000000-0000-0000-0000-0000000000e1', NULL, 0, now(),
        pg_temp.job('daily_review', NULL));
DO $$ BEGIN RAISE NOTICE 'PASS: a daily review job has no run'; END $$;
SELECT pg_temp.rv_expect_error(
    $$INSERT INTO maintenance_jobs (job_id, tenant_id, run_id, revision, due_at, record)
      VALUES (repeat('d', 64), '00000000-0000-0000-0000-0000000000e1', NULL, 0, now(),
              jsonb_set(pg_temp.job('write_readback', NULL), '{job_id}', to_jsonb(repeat('d', 64))))$$,
    '23514', 'only the review kind may omit its run');
SELECT pg_temp.rv_expect_error(
    $$INSERT INTO maintenance_jobs (job_id, tenant_id, run_id, revision, due_at, record)
      VALUES (repeat('d', 64), '00000000-0000-0000-0000-0000000000e1',
              '00000000-0000-0000-0000-0000000000e4', 0, now(),
              jsonb_set(pg_temp.job('daily_review', '00000000-0000-0000-0000-0000000000e4'),
                        '{job_id}', to_jsonb(repeat('d', 64))))$$,
    '23514', 'a review job cannot name a run');
SELECT pg_temp.rv_expect_error(
    $$INSERT INTO maintenance_jobs (job_id, tenant_id, run_id, revision, due_at, record)
      VALUES (repeat('d', 64), '00000000-0000-0000-0000-0000000000e1',
              '00000000-0000-0000-0000-0000000000e4', 0, now(),
              jsonb_set(pg_temp.job('write_readback', NULL), '{job_id}', to_jsonb(repeat('d', 64))))$$,
    '23514', 'the stored record must name the indexed run');

INSERT INTO review_items (item_id, tenant_id, day, run_id, step_key, action_hash, reason, key_id,
                          created_at)
VALUES (repeat('1', 64), '00000000-0000-0000-0000-0000000000e1', '2026-11-15',
        '00000000-0000-0000-0000-0000000000e4', 'write', repeat('a', 64), 'sampled_low',
        '0123456789abcdef', now());
DO $$ BEGIN RAISE NOTICE 'PASS: a sampled item records its key id'; END $$;
SELECT pg_temp.rv_expect_error(
    $$INSERT INTO review_items (item_id, tenant_id, day, run_id, reason, created_at)
      VALUES (repeat('2', 64), '00000000-0000-0000-0000-0000000000e1', '2026-11-15',
              '00000000-0000-0000-0000-0000000000e4', 'sampled_low', now())$$,
    '23514', 'a sample without a key id is refused');
SELECT pg_temp.rv_expect_error(
    $$INSERT INTO review_items (item_id, tenant_id, day, run_id, reason, created_at)
      VALUES (repeat('2', 64), '00000000-0000-0000-0000-0000000000e1', '2026-11-15',
              '00000000-0000-0000-0000-0000000000e4', 'looks_odd', now())$$,
    '23514', 'unknown reasons are refused');
SELECT pg_temp.rv_expect_error(
    $$UPDATE review_items SET status = 'confirmed'$$,
    '23514', 'a verdict needs its decider and time');
SELECT pg_temp.rv_expect_error(
    $$INSERT INTO review_items (item_id, tenant_id, day, run_id, reason, created_at)
      VALUES (repeat('2', 64), '00000000-0000-0000-0000-0000000000e1', '2026-11-15',
              '00000000-0000-0000-0000-000000000099', 'audit_mismatch', now())$$,
    '23503', 'an item names a run of its own tenant');

SET LOCAL ROLE gawe_app;
UPDATE review_items SET status = 'confirmed',
    decided_by = '00000000-0000-0000-0000-0000000000e3', decided_at = now(), event_seq = 3;
SELECT count(*) FROM review_items;
RESET ROLE;
DO $$ BEGIN RAISE NOTICE 'PASS: the app role can add items and record verdicts'; END $$;
SET LOCAL ROLE gawe_app;
SELECT pg_temp.rv_expect_error(
    $$UPDATE review_items SET reason = 'superseded'$$, '42501',
    'the app role cannot rewrite what an item found');
SELECT pg_temp.rv_expect_error(
    $$DELETE FROM review_items$$, '42501', 'the app role cannot delete review items');
RESET ROLE;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'audit_events_type_ts_idx') THEN
        RAISE EXCEPTION 'FAIL: audit events are indexed by type and time';
    END IF;
    RAISE NOTICE 'PASS: audit events are indexed by type and time';
END $$;
ROLLBACK;
