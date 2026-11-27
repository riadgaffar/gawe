-- Upgrade both the original S2 table and the expanded S3 fresh-install table.
DO $$ DECLARE c record; BEGIN
    FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'undo_records'::regclass
        AND contype = 'c' AND (pg_get_constraintdef(oid) LIKE '%''armed''%'
                              OR pg_get_constraintdef(oid) LIKE '%''unavailable''%')
    LOOP EXECUTE format('ALTER TABLE undo_records DROP CONSTRAINT %I', c.conname); END LOOP;
END $$;
ALTER TABLE undo_records
    ADD CONSTRAINT undo_state_valid CHECK (state IN
        ('pending', 'armed', 'unavailable', 'requested', 'used', 'failed')),
    ADD CONSTRAINT undo_result_required CHECK ((state IN
        ('armed', 'requested', 'used', 'failed')) = (record->>'result_version' IS NOT NULL)),
    ADD CONSTRAINT undo_reason_required CHECK ((state IN
        ('unavailable', 'failed')) = (record->>'reason' IS NOT NULL));
