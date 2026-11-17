-- M16c: a run can name the record its document came from (for IT access, a ticket). The app
-- resolves it; the server, not the caller, supplies the document text.
ALTER TABLE runs ADD COLUMN source_id text
    CHECK (source_id IS NULL OR source_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$');
