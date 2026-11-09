-- Schema behavior tests. Run by scripts/test_schema.sh against a throwaway database.
-- Every check prints "PASS: <label>" or aborts the script with "FAIL: <label>".
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

-- ---------------------------------------------------------------------- seed
INSERT INTO tenants (id, name) VALUES
    ('00000000-0000-0000-0000-0000000000a1', 'tenant-a'),
    ('00000000-0000-0000-0000-0000000000b1', 'tenant-b');
INSERT INTO users (id, tenant_id, email) VALUES
    ('00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-0000000000a1', 'alice@a.test'),
    ('00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-0000000000a1', 'bob@a.test'),
    ('00000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-0000000000b1', 'carol@b.test');
INSERT INTO runs (id, tenant_id, acting_user_id, objective, profile) VALUES
    ('00000000-0000-0000-0000-000000000f01', '00000000-0000-0000-0000-0000000000a1',
     '00000000-0000-0000-0000-000000000001', 'review contract', 'contract_review'),
    ('00000000-0000-0000-0000-000000000f02', '00000000-0000-0000-0000-0000000000a1',
     '00000000-0000-0000-0000-000000000001', 'review contract 2', 'contract_review');
INSERT INTO plans (id, run_id, version, steps, planner_model) VALUES
    ('00000000-0000-0000-0000-000000000e01', '00000000-0000-0000-0000-000000000f01', 1, '[]', 'fake');
INSERT INTO steps (id, run_id, plan_id, step_key, tool_name, tool_version, args_template) VALUES
    ('00000000-0000-0000-0000-000000000d01', '00000000-0000-0000-0000-000000000f01',
     '00000000-0000-0000-0000-000000000e01', 's1', 'contracts.send_email', '1.0.0', '{}'),
    ('00000000-0000-0000-0000-000000000d02', '00000000-0000-0000-0000-000000000f01',
     '00000000-0000-0000-0000-000000000e01', 's2', 'contracts.send_email', '1.0.0', '{}');

-- ------------------------------------------------------------- audit: chain
SELECT pg_temp.expect_ok($$
    INSERT INTO audit_events VALUES ('00000000-0000-0000-0000-000000000f01', 1, 'run.created', 'alice',
        repeat('1',64), repeat('0',64), repeat('a',64), now())$$,
    'audit: genesis event accepted');

SELECT pg_temp.expect_ok($$
    INSERT INTO audit_events VALUES ('00000000-0000-0000-0000-000000000f01', 2, 'plan.created', 'planner',
        repeat('2',64), repeat('a',64), repeat('b',64), now())$$,
    'audit: second event extending the chain accepted');

SELECT pg_temp.expect_error($$
    INSERT INTO audit_events VALUES ('00000000-0000-0000-0000-000000000f02', 2, 'x', 'alice',
        repeat('1',64), repeat('0',64), repeat('c',64), now())$$,
    '23514', 'audit: first event of a run must be seq 1');

SELECT pg_temp.expect_error($$
    INSERT INTO audit_events VALUES ('00000000-0000-0000-0000-000000000f02', 1, 'x', 'alice',
        repeat('1',64), repeat('9',64), repeat('c',64), now())$$,
    '23514', 'audit: first event must carry the genesis prev_hash');

SELECT pg_temp.expect_error($$
    INSERT INTO audit_events VALUES ('00000000-0000-0000-0000-000000000f01', 3, 'x', 'alice',
        repeat('1',64), repeat('9',64), repeat('d',64), now())$$,
    '23514', 'audit: wrong prev_hash rejected');

SELECT pg_temp.expect_error($$
    INSERT INTO audit_events VALUES ('00000000-0000-0000-0000-000000000f01', 5, 'x', 'alice',
        repeat('1',64), repeat('b',64), repeat('d',64), now())$$,
    '23514', 'audit: sequence gap rejected');

SELECT pg_temp.expect_error($$
    INSERT INTO audit_events VALUES ('00000000-0000-0000-0000-000000000f01', 2, 'x', 'alice',
        repeat('1',64), repeat('a',64), repeat('d',64), now())$$,
    '23514', 'audit: duplicate seq (fork attempt) rejected');

SELECT pg_temp.expect_error($$
    INSERT INTO audit_events VALUES ('00000000-0000-0000-0000-000000000f01', 3, 'x', 'alice',
        repeat('1',64), repeat('b',64), 'not-a-hash', now())$$,
    '23514', 'audit: malformed hash rejected');

SELECT pg_temp.expect_error($$
    INSERT INTO audit_events VALUES ('00000000-0000-0000-0000-000000000f02', 1, 'x', 'alice',
        repeat('1',64), repeat('0',64), repeat('a',64), now())$$,
    '23505', 'audit: event hashes are globally unique');

SELECT pg_temp.expect_error($$
    INSERT INTO audit_events VALUES ('00000000-0000-0000-0000-00000000dead', 1, 'x', 'alice',
        repeat('1',64), repeat('0',64), repeat('e',64), now())$$,
    '23503', 'audit: events must reference an existing run');

SELECT pg_temp.expect_error($$
    INSERT INTO audit_events VALUES ('00000000-0000-0000-0000-000000000f02', 1, '', 'alice',
        repeat('1',64), repeat('0',64), repeat('f',64), now())$$,
    '23514', 'audit: empty event type rejected');

-- ------------------------------------------------------ audit: immutability
SELECT pg_temp.expect_error($$UPDATE audit_events SET actor = 'mallory'$$,
    '42501', 'audit: UPDATE blocked');
SELECT pg_temp.expect_error($$UPDATE audit_events SET hash = repeat('f',64) WHERE seq = 1$$,
    '42501', 'audit: UPDATE of the hash blocked');
SELECT pg_temp.expect_error($$DELETE FROM audit_events WHERE seq = 2$$,
    '42501', 'audit: DELETE blocked');
SELECT pg_temp.expect_error($$DELETE FROM audit_events$$,
    '42501', 'audit: bulk DELETE blocked');
SELECT pg_temp.expect_error($$TRUNCATE audit_events$$,
    '42501', 'audit: TRUNCATE blocked');

DO $$
BEGIN
    IF (SELECT count(*) FROM audit_events) <> 2 THEN
        RAISE EXCEPTION 'FAIL: audit rows changed despite blocked writes';
    END IF;
    RAISE NOTICE 'PASS: audit rows intact after all blocked writes';
END $$;

-- ----------------------------------------------------- approvals / authority
SELECT pg_temp.expect_error($$
    INSERT INTO approvals (approver_role, step_id, action_hash, requested_by, expires_at, decided_by, decision, decided_at)
    VALUES ('legal_reviewer', '00000000-0000-0000-0000-000000000d01', repeat('a',64),
            '00000000-0000-0000-0000-000000000001', now() + interval '1 hour',
            '00000000-0000-0000-0000-000000000001', 'approved', now())$$,
    '23514', 'approvals: requester cannot approve their own request');

SELECT pg_temp.expect_error($$
    INSERT INTO approvals (approver_role, step_id, action_hash, requested_by, expires_at, decision)
    VALUES ('legal_reviewer', '00000000-0000-0000-0000-000000000d01', repeat('a',64),
            '00000000-0000-0000-0000-000000000001', now() + interval '1 hour', 'approved')$$,
    '23514', 'approvals: a decision requires a decider and a time');

SELECT pg_temp.expect_error($$
    INSERT INTO approvals (approver_role, step_id, action_hash, requested_by, requested_at, expires_at)
    VALUES ('legal_reviewer', '00000000-0000-0000-0000-000000000d01', repeat('a',64),
            '00000000-0000-0000-0000-000000000001', now(), now() - interval '1 minute')$$,
    '23514', 'approvals: expiry must be after the request');

SELECT pg_temp.expect_ok($$
    INSERT INTO approvals (approver_role, step_id, action_hash, requested_by, expires_at, decided_by, decision, decided_at)
    VALUES ('legal_reviewer', '00000000-0000-0000-0000-000000000d01', repeat('a',64),
            '00000000-0000-0000-0000-000000000001', now() + interval '1 hour',
            '00000000-0000-0000-0000-000000000002', 'approved', now())$$,
    'approvals: a different user can approve');

SELECT pg_temp.expect_ok($$
    INSERT INTO approvals (approver_role, step_id, action_hash, requested_by, expires_at)
    VALUES ('legal_reviewer', '00000000-0000-0000-0000-000000000d02', repeat('b',64),
            '00000000-0000-0000-0000-000000000001', now() + interval '1 hour')$$,
    'approvals: one open request per step accepted');

SELECT pg_temp.expect_error($$
    INSERT INTO approvals (approver_role, step_id, action_hash, requested_by, expires_at)
    VALUES ('legal_reviewer', '00000000-0000-0000-0000-000000000d02', repeat('c',64),
            '00000000-0000-0000-0000-000000000001', now() + interval '1 hour')$$,
    '23505', 'approvals: a second open request for the same step rejected');

-- -------------------------------------------------------- policy decisions
SELECT pg_temp.expect_error($$
    INSERT INTO policy_decisions (step_id, action_hash, outcome, rule_ids, reason, signature)
    VALUES ('00000000-0000-0000-0000-000000000d01', repeat('a',64), 'needs_approval',
            '{r}', 'x', repeat('f',64))$$,
    '23514', 'decisions: needs_approval requires an approver_role');

SELECT pg_temp.expect_error($$
    INSERT INTO policy_decisions (step_id, action_hash, outcome, rule_ids, approver_role, reason, signature)
    VALUES ('00000000-0000-0000-0000-000000000d01', repeat('a',64), 'allow',
            '{r}', 'boss', 'x', repeat('f',64))$$,
    '23514', 'decisions: approver_role only allowed with needs_approval');

SELECT pg_temp.expect_ok($$
    INSERT INTO policy_decisions (step_id, action_hash, outcome, rule_ids, approver_role, reason, signature)
    VALUES ('00000000-0000-0000-0000-000000000d01', repeat('a',64), 'needs_approval',
            '{r}', 'boss', 'x', repeat('f',64))$$,
    'decisions: valid needs_approval accepted');

SELECT pg_temp.expect_error($$
    INSERT INTO policy_decisions (step_id, action_hash, outcome, rule_ids, reason, signature)
    VALUES ('00000000-0000-0000-0000-000000000d01', repeat('a',64), 'allow', '{r}', 'x', 'short')$$,
    '23514', 'decisions: malformed signature rejected');

-- --------------------------------------------------------------- steps / runs
SELECT pg_temp.expect_error($$
    UPDATE steps SET status = 'succeeded' WHERE id = '00000000-0000-0000-0000-000000000d01'$$,
    '23514', 'steps: a step past policy must carry an action_hash');

SELECT pg_temp.expect_ok($$
    UPDATE steps SET status = 'failed' WHERE id = '00000000-0000-0000-0000-000000000d01'$$,
    'steps: a step may fail before it was ever evaluated');

SELECT pg_temp.expect_error($$
    UPDATE steps SET status = 'teleported' WHERE id = '00000000-0000-0000-0000-000000000d01'$$,
    '23514', 'steps: unknown status rejected');

SELECT pg_temp.expect_error($$
    INSERT INTO runs (tenant_id, acting_user_id, objective, profile)
    VALUES ('00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-000000000003', 'x', 'p')$$,
    '23503', 'runs: acting user must belong to the run''s tenant');

SELECT pg_temp.expect_error($$
    UPDATE runs SET status = 'exploded' WHERE id = '00000000-0000-0000-0000-000000000f01'$$,
    '23514', 'runs: unknown status rejected');

-- ---------------------------------------------------------- executions / tools
SELECT pg_temp.expect_ok($$
    INSERT INTO executions (step_id, attempt, idempotency_key, status)
    VALUES ('00000000-0000-0000-0000-000000000d01', 1, 'key-1', 'started')$$,
    'executions: first attempt accepted');

SELECT pg_temp.expect_error($$
    INSERT INTO executions (step_id, attempt, idempotency_key, status)
    VALUES ('00000000-0000-0000-0000-000000000d01', 1, 'key-1', 'started')$$,
    '23505', 'executions: duplicate attempt number rejected');

SELECT pg_temp.expect_error($$
    INSERT INTO executions (step_id, attempt, idempotency_key, status, finished_at)
    VALUES ('00000000-0000-0000-0000-000000000d01', 2, 'key-1', 'started', now())$$,
    '23514', 'executions: a started attempt cannot have a finish time');

SELECT pg_temp.expect_error($$
    INSERT INTO executions (step_id, attempt, idempotency_key, status)
    VALUES ('00000000-0000-0000-0000-000000000d01', 2, 'key-1', 'succeeded')$$,
    '23514', 'executions: a finished attempt needs a finish time');

SELECT pg_temp.expect_error($$
    INSERT INTO tools (tenant_id, name, version, manifest, side_effect_class, risk_tier)
    VALUES ('00000000-0000-0000-0000-0000000000a1', 't', 'v1', '{}', 'read', 'low')$$,
    '23514', 'tools: version must be semver');

SELECT pg_temp.expect_error($$
    INSERT INTO tools (tenant_id, name, version, manifest, side_effect_class, risk_tier)
    VALUES ('00000000-0000-0000-0000-0000000000a1', 't', '1.0.0', '{}', 'read', 'extreme')$$,
    '23514', 'tools: risk tier must be a known value');
