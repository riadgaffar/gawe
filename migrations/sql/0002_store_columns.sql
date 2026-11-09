-- 0002: columns and a table the step/approval/execution stores need.
--   steps.error               why a step failed (short code, never tool output)
--   approvals.approver_role   the role the policy decision required; checked when deciding
--   executions.output_hash    SHA-256 of the canonical tool output for that attempt
--   step_outputs              the tool's output, written once and never changed
BEGIN;

ALTER TABLE steps ADD COLUMN error text;

ALTER TABLE approvals ADD COLUMN approver_role text;
-- Rows created before this migration take the role from the step's latest policy decision.
UPDATE approvals a
   SET approver_role = pd.approver_role
  FROM (SELECT DISTINCT ON (step_id) step_id, approver_role
          FROM policy_decisions
         WHERE approver_role IS NOT NULL
         ORDER BY step_id, evaluated_at DESC) pd
 WHERE pd.step_id = a.step_id;
-- Fails loudly if any pre-existing approval could not be given a role.
ALTER TABLE approvals ALTER COLUMN approver_role SET NOT NULL;
ALTER TABLE approvals ADD CONSTRAINT approvals_approver_role_nonempty
    CHECK (length(approver_role) > 0);

ALTER TABLE executions ADD COLUMN output_hash text
    CONSTRAINT executions_output_hash_hex CHECK (output_hash ~ '^[0-9a-f]{64}$');

CREATE TABLE step_outputs (
    step_id     uuid PRIMARY KEY REFERENCES steps(id),
    output      jsonb NOT NULL,
    output_hash text NOT NULL CHECK (output_hash ~ '^[0-9a-f]{64}$'),
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION step_outputs_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'step_outputs rows are immutable' USING ERRCODE = '42501';
END $$;

CREATE TRIGGER step_outputs_no_update
    BEFORE UPDATE ON step_outputs
    FOR EACH ROW EXECUTE FUNCTION step_outputs_immutable();

COMMIT;
