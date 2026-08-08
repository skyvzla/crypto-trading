CREATE TABLE exchange_symbols (
    symbol VARCHAR(32) PRIMARY KEY,
    pair VARCHAR(32) NOT NULL,
    contract_type VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    onboard_date TIMESTAMPTZ,
    delivery_date TIMESTAMPTZ,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_exchange_symbols_entry_lookup
    ON exchange_symbols(active, contract_type, status, delivery_date);
