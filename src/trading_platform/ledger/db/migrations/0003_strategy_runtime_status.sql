CREATE TABLE strategy_runtime_status (
    account_id VARCHAR(32) NOT NULL,
    strategy_id VARCHAR(64) NOT NULL,
    instance_id VARCHAR(128) NOT NULL,
    mode VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    entry_enabled BOOLEAN NOT NULL,
    halted BOOLEAN NOT NULL,
    halt_reason TEXT,
    gate_conditions JSONB NOT NULL DEFAULT '{}'::JSONB,
    started_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    stopped_at TIMESTAMPTZ,
    PRIMARY KEY (account_id, strategy_id)
);

CREATE INDEX idx_strategy_runtime_status_heartbeat
    ON strategy_runtime_status(heartbeat_at DESC);
