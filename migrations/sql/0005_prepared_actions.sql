-- Trusted preparation is kept outside the hash; each approval retains its immutable snapshot.
ALTER TABLE steps ADD COLUMN preparation jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(preparation) = 'object');
ALTER TABLE steps ADD COLUMN policy_decision jsonb
    CHECK (policy_decision IS NULL OR jsonb_typeof(policy_decision) = 'object');
ALTER TABLE approvals ADD COLUMN preparation jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(preparation) = 'object');
ALTER TABLE approvals ADD COLUMN resolved_args jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(resolved_args) = 'object');
-- Existing table grants cover the added columns. No raw arguments are added to audit events.
