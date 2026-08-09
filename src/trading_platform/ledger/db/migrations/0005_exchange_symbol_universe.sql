ALTER TABLE exchange_symbols
    ADD COLUMN base_asset VARCHAR(32),
    ADD COLUMN quote_asset VARCHAR(32),
    ADD COLUMN margin_asset VARCHAR(32),
    ADD COLUMN underlying_type VARCHAR(64),
    ADD COLUMN raw_metadata JSONB NOT NULL DEFAULT '{}'::JSONB;

CREATE TABLE exchange_categories (
    category_key VARCHAR(256) PRIMARY KEY,
    source VARCHAR(32) NOT NULL,
    category_type VARCHAR(32) NOT NULL
        CHECK (category_type IN ('CATEGORY', 'SUBCATEGORY')),
    code VARCHAR(96) NOT NULL,
    name VARCHAR(128) NOT NULL,
    parent_key VARCHAR(256) REFERENCES exchange_categories(category_key),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (category_type = 'CATEGORY' AND parent_key IS NULL)
        OR (category_type = 'SUBCATEGORY' AND parent_key IS NOT NULL)
    )
);

CREATE INDEX idx_exchange_categories_parent
    ON exchange_categories(source, parent_key, active);

CREATE TABLE exchange_symbol_categories (
    symbol VARCHAR(32) NOT NULL REFERENCES exchange_symbols(symbol),
    category_key VARCHAR(256) NOT NULL
        REFERENCES exchange_categories(category_key),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, category_key)
);

CREATE INDEX idx_exchange_symbol_categories_category
    ON exchange_symbol_categories(category_key, active, symbol);

CREATE TABLE exchange_symbol_sync_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    status VARCHAR(16) NOT NULL CHECK (status IN ('SUCCESS', 'FAILED')),
    last_attempt_at TIMESTAMPTZ NOT NULL,
    last_success_at TIMESTAMPTZ,
    synced_symbols INTEGER NOT NULL DEFAULT 0 CHECK (synced_symbols >= 0),
    last_error TEXT
);

CREATE TABLE symbol_global_admission (
    symbol VARCHAR(32) PRIMARY KEY REFERENCES exchange_symbols(symbol),
    enabled BOOLEAN NOT NULL,
    version BIGINT NOT NULL CHECK (version > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(128) NOT NULL,
    reason TEXT
);

CREATE TABLE symbol_global_admission_audit (
    id BIGSERIAL PRIMARY KEY,
    symbol VARCHAR(32) NOT NULL REFERENCES exchange_symbols(symbol),
    previous_enabled BOOLEAN,
    enabled BOOLEAN NOT NULL,
    version BIGINT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_by VARCHAR(128) NOT NULL,
    reason TEXT,
    UNIQUE (symbol, version)
);

CREATE INDEX idx_symbol_global_admission_audit_changed
    ON symbol_global_admission_audit(changed_at DESC);

CREATE TABLE strategy_category_admission (
    strategy_id VARCHAR(64) NOT NULL,
    category_key VARCHAR(256) NOT NULL
        REFERENCES exchange_categories(category_key),
    enabled BOOLEAN NOT NULL,
    version BIGINT NOT NULL CHECK (version > 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(128) NOT NULL,
    reason TEXT,
    PRIMARY KEY (strategy_id, category_key)
);

CREATE TABLE strategy_category_admission_audit (
    id BIGSERIAL PRIMARY KEY,
    strategy_id VARCHAR(64) NOT NULL,
    category_key VARCHAR(256) NOT NULL
        REFERENCES exchange_categories(category_key),
    previous_enabled BOOLEAN,
    enabled BOOLEAN NOT NULL,
    version BIGINT NOT NULL,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_by VARCHAR(128) NOT NULL,
    reason TEXT,
    UNIQUE (strategy_id, category_key, version)
);

CREATE INDEX idx_strategy_category_admission_lookup
    ON strategy_category_admission(strategy_id, enabled, category_key);

CREATE INDEX idx_strategy_category_admission_audit_changed
    ON strategy_category_admission_audit(changed_at DESC);
