ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS campaign_id VARCHAR(128);

ALTER TABLE trades
    ADD COLUMN IF NOT EXISTS campaign_id VARCHAR(128);

CREATE INDEX IF NOT EXISTS idx_orders_campaign
    ON orders(account_id, strategy_id, campaign_id)
    WHERE campaign_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trades_campaign_time
    ON trades(account_id, strategy_id, campaign_id, exchange_time)
    WHERE campaign_id IS NOT NULL;
