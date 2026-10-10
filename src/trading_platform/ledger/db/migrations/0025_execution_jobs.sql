CREATE TABLE strategy_execution_jobs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('entry', 'exit')),
    event_time BIGINT NOT NULL CHECK (event_time >= 0),
    intent JSONB NOT NULL CHECK (jsonb_typeof(intent) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'expired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    UNIQUE (account_id, client_order_id)
);
CREATE INDEX strategy_execution_jobs_pending
    ON strategy_execution_jobs (account_id, strategy_id, id)
    WHERE status = 'pending';
