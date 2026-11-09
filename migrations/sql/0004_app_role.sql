-- Migration 0004: a least-privilege role for the running application.
--
-- The application should not connect as the database owner. As `gawe_app` it can read everything,
-- write only the tables it needs, and only INSERT into audit_events: it cannot UPDATE, DELETE or
-- TRUNCATE the audit log (the triggers from 0001 already refuse that for every role, but a
-- privilege check stops the attempt earlier and survives someone disabling a trigger), and it
-- cannot change the schema.
--
-- The role is created WITHOUT a login or password here; a migration must never hold a secret.
-- `scripts/create_app_role.sh` (make app-role) turns on login with a password from the
-- environment. Any table added by a later migration needs its own GRANT: the schema test
-- (tests/sql/test_app_role.sql) fails if a table is left without SELECT.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gawe_app') THEN
        CREATE ROLE gawe_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO gawe_app;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO gawe_app;

-- Tables the stores update as a run progresses.
GRANT INSERT, UPDATE ON runs, steps, approvals, executions TO gawe_app;
-- Write-once tables: insert only.
GRANT INSERT ON plans, step_outputs, audit_events TO gawe_app;
