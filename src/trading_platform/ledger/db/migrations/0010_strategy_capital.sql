CREATE TABLE strategy_capital_state (
    account_id VARCHAR(64) NOT NULL,
    strategy_id VARCHAR(128) NOT NULL,
    initial_account_capital NUMERIC(38, 18) NOT NULL
        CHECK (initial_account_capital >= 0),
    initial_trading_capital NUMERIC(38, 18) NOT NULL
        CHECK (initial_trading_capital >= 0),
    profit_reinvest_ratio NUMERIC(20, 18) NOT NULL
        CHECK (profit_reinvest_ratio >= 0 AND profit_reinvest_ratio <= 1),
    minimum_trading_capital NUMERIC(38, 18) NOT NULL
        CHECK (minimum_trading_capital >= 0),
    account_capital NUMERIC(38, 18) NOT NULL,
    trading_capital NUMERIC(38, 18) NOT NULL
        CHECK (trading_capital >= 0),
    reserve_capital NUMERIC(38, 18) NOT NULL,
    capital_breached BOOLEAN NOT NULL DEFAULT FALSE,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (account_id, strategy_id),
    CHECK (initial_trading_capital <= initial_account_capital),
    CHECK (account_capital = trading_capital + reserve_capital)
);

CREATE TABLE strategy_capital_events (
    id UUID PRIMARY KEY,
    account_id VARCHAR(64) NOT NULL,
    strategy_id VARCHAR(128) NOT NULL,
    campaign_id VARCHAR(128),
    idempotency_key VARCHAR(256) NOT NULL CHECK (btrim(idempotency_key) <> ''),
    event_type VARCHAR(32) NOT NULL
        CHECK (event_type IN (
            'INITIALIZED', 'PROFIT_SETTLED', 'LOSS_SETTLED',
            'CAPITAL_BREACH', 'RECONCILED'
        )),
    net_pnl NUMERIC(38, 18) NOT NULL DEFAULT 0,
    trading_capital_before NUMERIC(38, 18) NOT NULL,
    trading_capital_after NUMERIC(38, 18) NOT NULL
        CHECK (trading_capital_after >= 0),
    reserve_capital_before NUMERIC(38, 18) NOT NULL,
    reserve_capital_after NUMERIC(38, 18) NOT NULL,
    account_capital_before NUMERIC(38, 18) NOT NULL,
    account_capital_after NUMERIC(38, 18) NOT NULL,
    reinvested_profit NUMERIC(38, 18) NOT NULL DEFAULT 0
        CHECK (reinvested_profit >= 0),
    reserve_consumed NUMERIC(38, 18) NOT NULL DEFAULT 0
        CHECK (reserve_consumed >= 0),
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (account_id, strategy_id, idempotency_key),
    CHECK (account_capital_after = trading_capital_after + reserve_capital_after)
);

CREATE INDEX idx_strategy_capital_events_lookup
    ON strategy_capital_events(account_id, strategy_id, occurred_at DESC);
