-- Ledger schema. Existing deployments require an explicit migration.

CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(32) NOT NULL,
    strategy_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    order_id VARCHAR(64) NOT NULL,
    client_order_id VARCHAR(64) NOT NULL UNIQUE,
    side VARCHAR(16) NOT NULL,
    order_type VARCHAR(16) NOT NULL,
    position_side VARCHAR(16),
    quantity DECIMAL(20, 8) NOT NULL,
    price DECIMAL(20, 8),
    stop_price DECIMAL(20, 8),
    status VARCHAR(32) NOT NULL,
    filled_quantity DECIMAL(20, 8) NOT NULL DEFAULT 0,
    avg_fill_price DECIMAL(20, 8),
    commission DECIMAL(20, 8),
    commission_asset VARCHAR(16),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exchange_created_at TIMESTAMPTZ,
    filled_at TIMESTAMPTZ,
    CONSTRAINT orders_account_symbol_order_id_key
        UNIQUE (account_id, symbol, order_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_created_at
    ON orders(created_at DESC);

CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(32) NOT NULL,
    strategy_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    trade_id VARCHAR(64) NOT NULL,
    order_id VARCHAR(64) NOT NULL,
    client_order_id VARCHAR(64) NOT NULL,
    side VARCHAR(16) NOT NULL,
    position_side VARCHAR(16),
    quantity DECIMAL(20, 8) NOT NULL,
    price DECIMAL(20, 8) NOT NULL,
    quote_quantity DECIMAL(20, 8) NOT NULL,
    commission DECIMAL(20, 8) NOT NULL,
    commission_asset VARCHAR(16) NOT NULL,
    realized_pnl DECIMAL(20, 8),
    is_maker BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exchange_time TIMESTAMPTZ NOT NULL,
    CONSTRAINT trades_account_symbol_trade_id_key
        UNIQUE (account_id, symbol, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_trades_created_at
    ON trades(created_at DESC);

CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(32) NOT NULL,
    strategy_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    position_side VARCHAR(16) NOT NULL,
    quantity DECIMAL(20, 8) NOT NULL,
    entry_price DECIMAL(20, 8) NOT NULL,
    mark_price DECIMAL(20, 8),
    unrealized_pnl DECIMAL(20, 8),
    liquidation_price DECIMAL(20, 8),
    leverage INTEGER,
    margin_type VARCHAR(16),
    isolated_margin DECIMAL(20, 8),
    exchange_time TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT positions_account_strategy_symbol_side_key
        UNIQUE (account_id, strategy_id, symbol, position_side)
);

ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS exchange_time TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_positions_updated_at
    ON positions(updated_at DESC);

CREATE TABLE IF NOT EXISTS subcategory_admission (
    subcategory VARCHAR(64) PRIMARY KEY,
    enabled BOOLEAN NOT NULL,
    version BIGINT NOT NULL CHECK (version > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(128) NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS subcategory_admission_audit (
    id BIGSERIAL PRIMARY KEY,
    subcategory VARCHAR(64) NOT NULL,
    previous_enabled BOOLEAN,
    enabled BOOLEAN NOT NULL,
    version BIGINT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_by VARCHAR(128) NOT NULL,
    reason TEXT,
    UNIQUE (subcategory, version)
);

CREATE INDEX IF NOT EXISTS idx_subcategory_audit_changed_at
    ON subcategory_admission_audit(changed_at DESC);

CREATE TABLE IF NOT EXISTS strategy_audit_events (
    id BIGSERIAL PRIMARY KEY,
    event_key CHAR(64) NOT NULL UNIQUE,
    account_id VARCHAR(32) NOT NULL,
    event_time BIGINT NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    strategy_id VARCHAR(64) NOT NULL,
    campaign_id VARCHAR(128),
    details JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_strategy_audit_events_lookup
    ON strategy_audit_events(account_id, strategy_id, symbol, event_time DESC);
