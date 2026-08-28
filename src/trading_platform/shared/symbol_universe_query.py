"""交易对有效集合及其决策事实的共享只读查询。"""

SYMBOL_UNIVERSE_MAX_SYNC_AGE_HOURS = 36

# 这个 CTE 是实际交易池和 Web 预览共同的唯一规则源。调用方依次传入退市冻结窗口、
# 可选 strategy_id；策略分类默认只允许 COIN，其他顶层 Category 默认关闭。策略的显式
# 控制记录优先于该默认值；strategy_id 为 NULL 时不应用策略分类默认值。
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
                    COALESCE(category_control.category_key, category.category_key)
                    ORDER BY category.category_key
                )
                FROM exchange_symbol_categories AS assignment
                JOIN exchange_categories AS category
                  ON category.category_key = assignment.category_key
                 AND category.active = TRUE
                LEFT JOIN strategy_category_admission AS category_control
                  ON category_control.category_key = assignment.category_key
                 AND category_control.strategy_id = %s
                LEFT JOIN exchange_categories AS parent_category
                  ON parent_category.category_key = category.parent_key
                 AND parent_category.active = TRUE
                WHERE assignment.symbol = symbol.symbol
                  AND assignment.active = TRUE
                  AND (
                      category_control.enabled = FALSE
                      OR (
                          NULLIF(BTRIM(%s), '') IS NOT NULL
                          AND
                          category_control.category_key IS NULL
                          AND NOT (
                              (category.category_type = 'CATEGORY'
                               AND UPPER(BTRIM(category.code)) = 'COIN')
                              OR
                              (category.category_type = 'SUBCATEGORY'
                               AND UPPER(BTRIM(parent_category.code)) = 'COIN')
                          )
                      )
                  )
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
