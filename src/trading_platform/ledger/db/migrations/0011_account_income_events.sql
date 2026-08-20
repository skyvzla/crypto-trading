CREATE TABLE account_income_events (
    account_id VARCHAR(64) NOT NULL CHECK (btrim(account_id) <> ''),
    transaction_id BIGINT NOT NULL CHECK (transaction_id >= 0),
    income_type VARCHAR(64) NOT NULL CHECK (btrim(income_type) <> ''),
    symbol VARCHAR(32) NOT NULL,
    asset VARCHAR(16) NOT NULL CHECK (btrim(asset) <> ''),
    amount NUMERIC(38, 18) NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    raw JSONB NOT NULL CHECK (jsonb_typeof(raw) = 'object'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_id, income_type, transaction_id)
);

CREATE INDEX idx_account_income_events_account_symbol_time
    ON account_income_events(account_id, symbol, event_time DESC);
