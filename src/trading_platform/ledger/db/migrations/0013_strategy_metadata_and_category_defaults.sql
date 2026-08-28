-- Reports created before strategy_id was persisted under universe used the
-- implementation path in fixed.strategy. Recover the operational Spike id so
-- existing Web research rows and their replay links stop showing unknown.
UPDATE backtest_researches
SET strategy_id = 'spike_short'
WHERE LOWER(BTRIM(strategy_id)) IN ('unknown', 'unk')
  AND (
      LOWER(BTRIM(config #>> '{universe,strategy_id}')) IN ('spike_short', 'spike-short')
      OR LOWER(BTRIM(config #>> '{fixed,strategy}')) LIKE 'trading_platform.strategies.spike.%'
      OR LOWER(BTRIM(source_metadata #>> '{summary_parameters,strategy}'))
          LIKE 'trading_platform.strategies.spike.%'
  );

-- Category admission is a strategy-scoped deny list. Materialize the default
-- for known strategies so the Web switch state and the effective-universe
-- query agree immediately; explicit existing controls always win.
WITH strategy_ids AS (
    SELECT 'spike_short'::VARCHAR(64) AS strategy_id
    UNION
    SELECT DISTINCT BTRIM(strategy_id)
    FROM strategy_runtime_status
    WHERE BTRIM(strategy_id) <> ''
    UNION
    SELECT DISTINCT BTRIM(strategy_id)
    FROM backtest_researches
    WHERE BTRIM(strategy_id) NOT IN ('', 'unknown', 'unk')
), inserted AS (
    INSERT INTO strategy_category_admission (
        strategy_id, category_key, enabled, version, updated_by, reason
    )
    SELECT strategies.strategy_id, category.category_key, FALSE, 1,
        'system-default', 'default category admission: only COIN enabled'
    FROM strategy_ids AS strategies
    CROSS JOIN exchange_categories AS category
    WHERE category.source = 'BINANCE'
      AND category.active = TRUE
      AND category.category_type = 'CATEGORY'
      AND UPPER(BTRIM(category.code)) <> 'COIN'
    ON CONFLICT (strategy_id, category_key) DO NOTHING
    RETURNING strategy_id, category_key, enabled, version, updated_by, reason
)
INSERT INTO strategy_category_admission_audit (
    strategy_id, category_key, previous_enabled, enabled, version,
    changed_by, reason
)
SELECT strategy_id, category_key, NULL, enabled, version, updated_by, reason
FROM inserted;
