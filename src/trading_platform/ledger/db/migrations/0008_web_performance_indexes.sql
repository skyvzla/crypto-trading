-- Web ledger lists and Campaign performance always constrain account/time.
-- The partial index keeps campaign selection and subsequent grouping bounded to
-- rows that can participate in Campaign-level metrics.
CREATE INDEX idx_trades_account_exchange_time
    ON trades(account_id, exchange_time DESC);

CREATE INDEX idx_trades_campaign_performance
    ON trades(account_id, strategy_id, symbol, exchange_time DESC)
    WHERE campaign_id IS NOT NULL;
