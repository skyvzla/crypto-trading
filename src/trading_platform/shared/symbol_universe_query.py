"""交易对有效集合及其决策事实的共享只读查询。"""

SYMBOL_UNIVERSE_MAX_SYNC_AGE_HOURS = 36

# 这个 CTE 是实际交易池和 Web 预览共同的唯一规则源。调用方依次传入退市冻结窗口、
# 可选 strategy_id；strategy_id 为 NULL 时 blocked_category_keys 自然为空，保持 D-031
# 的“空配置默认允许”语义。
SYMBOL_UNIVERSE_EVALUATED_CTES_SQL = f"""
    universe_decisions AS (
        SELECT symbol.symbol,
            EXISTS (
                SELECT 1 FROM exchange_symbol_sync_state AS sync_state
                WHERE sync_state.singleton = TRUE
                  AND sync_state.status = 'SUCCESS'
                  AND sync_state.last_success_at >= NOW()
                      - INTERVAL '{SYMBOL_UNIVERSE_MAX_SYNC_AGE_HOURS} hours'
            ) AS sync_ready,
            symbol.active AS symbol_active,
            symbol.contract_type = 'PERPETUAL' AS perpetual_contract,
            symbol.status = 'TRADING' AS trading_status,
            symbol.onboard_date IS NOT NULL
                AND symbol.onboard_date <= NOW() AS onboarded,
            symbol.delivery_date IS NOT NULL
                AND symbol.delivery_date > NOW() + %s AS delivery_window_open,
            NOT EXISTS (
                SELECT 1
                FROM symbol_global_admission AS global_control
                WHERE global_control.symbol = symbol.symbol
                  AND global_control.enabled = FALSE
            ) AS global_enabled,
            COALESCE((
                SELECT ARRAY_AGG(
                    category_control.category_key
                    ORDER BY category_control.category_key
                )
                FROM exchange_symbol_categories AS assignment
                JOIN strategy_category_admission AS category_control
                  ON category_control.category_key = assignment.category_key
                WHERE assignment.symbol = symbol.symbol
                  AND assignment.active = TRUE
                  AND category_control.strategy_id = %s
                  AND category_control.enabled = FALSE
            ), ARRAY[]::VARCHAR[]) AS blocked_category_keys
        FROM exchange_symbols AS symbol
    ),
    evaluated_universe AS (
        SELECT decisions.*,
            decisions.sync_ready
              AND decisions.symbol_active
              AND decisions.perpetual_contract
              AND decisions.trading_status
              AND decisions.onboarded
              AND decisions.delivery_window_open
              AND decisions.global_enabled
              AND CARDINALITY(decisions.blocked_category_keys) = 0 AS effective
        FROM universe_decisions AS decisions
    )
"""

EFFECTIVE_SYMBOL_UNIVERSE_SQL = f"""
    WITH {SYMBOL_UNIVERSE_EVALUATED_CTES_SQL}
    SELECT symbol
    FROM evaluated_universe
    WHERE effective = TRUE
    ORDER BY symbol
"""
