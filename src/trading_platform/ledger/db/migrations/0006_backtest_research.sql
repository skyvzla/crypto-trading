CREATE TABLE backtest_researches (
    id UUID PRIMARY KEY,
    source_key CHAR(64) NOT NULL UNIQUE,
    name TEXT NOT NULL,
    strategy_id VARCHAR(64) NOT NULL,
    strategy_version VARCHAR(64),
    status VARCHAR(32) NOT NULL,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    config JSONB NOT NULL DEFAULT '{}'::JSONB,
    source_metadata JSONB NOT NULL DEFAULT '{}'::JSONB,
    report_path TEXT,
    symbol_count INTEGER NOT NULL DEFAULT 0,
    run_count INTEGER NOT NULL DEFAULT 0,
    trade_count INTEGER NOT NULL DEFAULT 0,
    win_count INTEGER NOT NULL DEFAULT 0,
    net_pnl NUMERIC NOT NULL DEFAULT 0,
    win_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_backtest_researches_imported_at
    ON backtest_researches(imported_at DESC);

CREATE TABLE backtest_runs (
    research_id UUID NOT NULL REFERENCES backtest_researches(id) ON DELETE CASCADE,
    run_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    parameters JSONB NOT NULL DEFAULT '{}'::JSONB,
    summary JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (research_id, run_id)
);

CREATE INDEX idx_backtest_runs_research_symbol
    ON backtest_runs(research_id, symbol);

CREATE TABLE backtest_trades (
    id UUID PRIMARY KEY,
    research_id UUID NOT NULL REFERENCES backtest_researches(id) ON DELETE CASCADE,
    run_id VARCHAR(128) NOT NULL,
    trade_id VARCHAR(160) NOT NULL,
    campaign_id VARCHAR(160),
    symbol VARCHAR(32) NOT NULL,
    side VARCHAR(16) NOT NULL,
    signal_time BIGINT,
    entry_time BIGINT,
    exit_time BIGINT,
    entry_price NUMERIC,
    exit_price NUMERIC,
    entry_quantity NUMERIC,
    entry_notional NUMERIC,
    entry_fill_count INTEGER,
    exit_fill_count INTEGER,
    gross_pnl NUMERIC,
    commission NUMERIC,
    net_pnl NUMERIC,
    net_return DOUBLE PRECISION,
    winner BOOLEAN,
    status VARCHAR(32),
    exit_reason VARCHAR(128),
    parameters JSONB NOT NULL DEFAULT '{}'::JSONB,
    strategy_data JSONB NOT NULL DEFAULT '{}'::JSONB,
    UNIQUE (research_id, run_id, trade_id),
    FOREIGN KEY (research_id, run_id)
        REFERENCES backtest_runs(research_id, run_id) ON DELETE CASCADE
);

CREATE INDEX idx_backtest_trades_research_symbol_time
    ON backtest_trades(research_id, symbol, entry_time DESC);
CREATE INDEX idx_backtest_trades_research_pnl
    ON backtest_trades(research_id, net_pnl);
CREATE INDEX idx_backtest_trades_research_winner
    ON backtest_trades(research_id, winner);

CREATE TABLE backtest_orders (
    research_id UUID NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    order_id VARCHAR(160) NOT NULL,
    campaign_id VARCHAR(160),
    symbol VARCHAR(32) NOT NULL,
    side VARCHAR(16),
    price NUMERIC,
    quantity NUMERIC,
    status VARCHAR(32),
    created_at BIGINT,
    fill_time BIGINT,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (research_id, run_id, order_id),
    FOREIGN KEY (research_id, run_id)
        REFERENCES backtest_runs(research_id, run_id) ON DELETE CASCADE
);

CREATE TABLE backtest_fills (
    research_id UUID NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    fill_id VARCHAR(160) NOT NULL,
    order_id VARCHAR(160) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    side VARCHAR(16),
    price NUMERIC,
    quantity NUMERIC,
    commission NUMERIC,
    fill_time BIGINT,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    PRIMARY KEY (research_id, run_id, fill_id),
    FOREIGN KEY (research_id, run_id)
        REFERENCES backtest_runs(research_id, run_id) ON DELETE CASCADE
);

CREATE INDEX idx_backtest_fills_lookup
    ON backtest_fills(research_id, run_id, order_id, fill_time);

CREATE TABLE backtest_events (
    id BIGSERIAL PRIMARY KEY,
    research_id UUID NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    campaign_id VARCHAR(160),
    symbol VARCHAR(32) NOT NULL,
    event_time BIGINT NOT NULL,
    event_type VARCHAR(96) NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::JSONB,
    FOREIGN KEY (research_id, run_id)
        REFERENCES backtest_runs(research_id, run_id) ON DELETE CASCADE
);

CREATE INDEX idx_backtest_events_trade_timeline
    ON backtest_events(research_id, run_id, campaign_id, event_time);

CREATE TABLE backtest_reports (
    research_id UUID NOT NULL REFERENCES backtest_researches(id) ON DELETE CASCADE,
    report_type VARCHAR(128) NOT NULL,
    title TEXT NOT NULL,
    category VARCHAR(64) NOT NULL DEFAULT 'analysis',
    description TEXT,
    columns JSONB NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (research_id, report_type)
);

CREATE TABLE backtest_report_rows (
    research_id UUID NOT NULL,
    report_type VARCHAR(128) NOT NULL,
    row_index INTEGER NOT NULL,
    data JSONB NOT NULL,
    PRIMARY KEY (research_id, report_type, row_index),
    FOREIGN KEY (research_id, report_type)
        REFERENCES backtest_reports(research_id, report_type) ON DELETE CASCADE
);

CREATE TABLE backtest_strategy_schemas (
    strategy_id VARCHAR(64) NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    descriptor JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (strategy_id, schema_version)
);
