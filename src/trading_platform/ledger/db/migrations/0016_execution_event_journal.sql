-- Immutable execution facts shared by live, testnet, and replay workers.
CREATE TABLE IF NOT EXISTS execution_event_journal (
    id BIGSERIAL PRIMARY KEY,
    event_id VARCHAR(128) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    sequence BIGINT NOT NULL CHECK (sequence >= 1),
    event_time BIGINT NOT NULL CHECK (event_time >= 0),
    event_type VARCHAR(128) NOT NULL,
    source VARCHAR(64) NOT NULL,
    severity VARCHAR(32) NOT NULL,
    account_id VARCHAR(64) NOT NULL,
    strategy_id VARCHAR(128) NOT NULL,
    trace_id VARCHAR(128) NOT NULL,
    causation_id VARCHAR(128),
    symbol VARCHAR(32),
    campaign_id VARCHAR(128),
    client_order_id VARCHAR(128),
    exchange_order_id VARCHAR(128),
    details JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(details) = 'object'),
    payload_hash CHAR(64) NOT NULL
        CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT execution_event_journal_event_id_key UNIQUE (event_id),
    CONSTRAINT execution_event_journal_run_sequence_key UNIQUE (run_id, sequence)
);

-- The journal is append-only.  Keep this guard in the database so a direct
-- SQL client cannot accidentally mutate or remove an incident fact.
CREATE OR REPLACE FUNCTION prevent_execution_event_journal_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'execution_event_journal is append-only';
END;
$$;

DROP TRIGGER IF EXISTS execution_event_journal_no_update_delete
    ON execution_event_journal;
CREATE TRIGGER execution_event_journal_no_update_delete
    BEFORE UPDATE OR DELETE ON execution_event_journal
    FOR EACH ROW EXECUTE FUNCTION prevent_execution_event_journal_mutation();

CREATE INDEX IF NOT EXISTS idx_execution_event_journal_account_time
    ON execution_event_journal(account_id, event_time, id);
CREATE INDEX IF NOT EXISTS idx_execution_event_journal_strategy_time
    ON execution_event_journal(strategy_id, event_time, id);
CREATE INDEX IF NOT EXISTS idx_execution_event_journal_run_sequence
    ON execution_event_journal(run_id, sequence, id);
CREATE INDEX IF NOT EXISTS idx_execution_event_journal_trace_time
    ON execution_event_journal(trace_id, event_time, id);
CREATE INDEX IF NOT EXISTS idx_execution_event_journal_event_type_time
    ON execution_event_journal(event_type, event_time, id);
CREATE INDEX IF NOT EXISTS idx_execution_event_journal_source_time
    ON execution_event_journal(source, event_time, id);
CREATE INDEX IF NOT EXISTS idx_execution_event_journal_symbol_time
    ON execution_event_journal(symbol, event_time, id)
    WHERE symbol IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_execution_event_journal_campaign_time
    ON execution_event_journal(campaign_id, event_time, id)
    WHERE campaign_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_execution_event_journal_client_order_time
    ON execution_event_journal(client_order_id, event_time, id)
    WHERE client_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_execution_event_journal_exchange_order_time
    ON execution_event_journal(exchange_order_id, event_time, id)
    WHERE exchange_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_execution_event_journal_event_time
    ON execution_event_journal(event_time, id);
