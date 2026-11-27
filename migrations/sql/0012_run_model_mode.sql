-- M29: how the scripted demo model behaves for a run, when the API runs it. Existing runs, and
-- every run under a real model, have none. A nullable column with no default is added without
-- rewriting the table; the limited gawe_app role's table-level grants (0004) cover it.
ALTER TABLE runs ADD COLUMN model_mode text
    CONSTRAINT runs_model_mode_valid CHECK (model_mode IS NULL OR model_mode IN ('honest', 'fooled'));
