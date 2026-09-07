-- The journal is append-only, including whole-table operations.
-- Keep this migration idempotent for local schema repair and test reruns.
DROP TRIGGER IF EXISTS execution_event_journal_no_truncate
    ON execution_event_journal;
CREATE TRIGGER execution_event_journal_no_truncate
    BEFORE TRUNCATE ON execution_event_journal
    FOR EACH STATEMENT EXECUTE FUNCTION prevent_execution_event_journal_mutation();
