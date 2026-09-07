-- Stable domain identity for snapshot lifecycle facts.  event_id/run_id and
-- sequence are process-local and therefore cannot deduplicate an outbox replay
-- after a restart.  Other execution events remain keyed only by event_id.
ALTER TABLE execution_event_journal
    ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(256);

CREATE UNIQUE INDEX IF NOT EXISTS execution_event_journal_snapshot_domain_key
    ON execution_event_journal(event_type, idempotency_key)
    WHERE event_type IN ('market.snapshot_completed', 'market.snapshot_failed')
      AND idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_execution_event_journal_idempotency_key
    ON execution_event_journal(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
