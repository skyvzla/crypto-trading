-- Metadata for immutable, per-Campaign 1s candle snapshots.
-- The candle payload lives in Parquet; PostgreSQL stores its identity,
-- lifecycle and integrity facts so the API can locate and audit it.
CREATE TABLE IF NOT EXISTS campaign_candle_snapshots (
    snapshot_id VARCHAR(128) PRIMARY KEY,
    account_id VARCHAR(64) NOT NULL,
    strategy_id VARCHAR(128) NOT NULL,
    campaign_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    signal_time_ms BIGINT NOT NULL CHECK (signal_time_ms >= 0),
    window_start_ms BIGINT NOT NULL CHECK (window_start_ms >= 0),
    window_end_ms BIGINT NOT NULL
        CHECK (window_end_ms > window_start_ms),
    status VARCHAR(16) NOT NULL DEFAULT 'collecting'
        CHECK (status IN ('collecting', 'completed', 'failed')),
    coverage JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(coverage) = 'object'),
    gaps JSONB NOT NULL DEFAULT '[]'::JSONB
        CHECK (jsonb_typeof(gaps) = 'array'),
    parquet_relative_path TEXT,
    parquet_sha256 CHAR(64)
        CHECK (parquet_sha256 IS NULL OR parquet_sha256 ~ '^[0-9a-f]{64}$'),
    row_count BIGINT CHECK (row_count IS NULL OR row_count >= 0),
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    aggregation_version INTEGER NOT NULL CHECK (aggregation_version > 0),
    release_hash CHAR(64) NOT NULL CHECK (release_hash ~ '^[0-9a-f]{64}$'),
    failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    CONSTRAINT campaign_candle_snapshots_window_order
        CHECK (window_start_ms <= signal_time_ms AND signal_time_ms < window_end_ms),
    CONSTRAINT campaign_candle_snapshots_relative_path
        CHECK (
            parquet_relative_path IS NULL
            OR (
                parquet_relative_path <> ''
                AND parquet_relative_path !~ '^[\\/]'
                AND parquet_relative_path !~ '(^|[\\/])\\.\\.([\\/]|$)'
            )
        ),
    CONSTRAINT campaign_candle_snapshots_completed_payload
        CHECK (
            status <> 'completed'
            OR (
                parquet_relative_path IS NOT NULL
                AND parquet_sha256 IS NOT NULL
                AND row_count IS NOT NULL
                AND completed_at IS NOT NULL
            )
        ),
    CONSTRAINT campaign_candle_snapshots_failed_reason
        CHECK (status <> 'failed' OR NULLIF(BTRIM(failure_reason), '') IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_campaign_candle_snapshots_campaign
    ON campaign_candle_snapshots(
        account_id, strategy_id, symbol, campaign_id, created_at DESC
    );
CREATE INDEX IF NOT EXISTS idx_campaign_candle_snapshots_status
    ON campaign_candle_snapshots(status, updated_at DESC);
