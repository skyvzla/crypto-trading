"""交易对有效集合的共享只读查询。"""

EFFECTIVE_SYMBOL_UNIVERSE_SQL = """
    SELECT symbol.symbol
    FROM exchange_symbols AS symbol
    WHERE EXISTS (
          SELECT 1 FROM exchange_symbol_sync_state AS sync_state
          WHERE sync_state.singleton = TRUE
            AND sync_state.status = 'SUCCESS'
            AND sync_state.last_success_at >= NOW() - INTERVAL '36 hours'
      )
      AND symbol.active = TRUE
      AND symbol.contract_type = 'PERPETUAL'
      AND symbol.status = 'TRADING'
      AND symbol.onboard_date IS NOT NULL
      AND symbol.onboard_date <= NOW()
      AND symbol.delivery_date IS NOT NULL
      AND symbol.delivery_date > NOW() + %s
      AND NOT EXISTS (
          SELECT 1
          FROM symbol_global_admission AS global_control
          WHERE global_control.symbol = symbol.symbol
            AND global_control.enabled = FALSE
      )
      AND (
          CAST(%s AS VARCHAR) IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM exchange_symbol_categories AS assignment
              JOIN strategy_category_admission AS category_control
                ON category_control.category_key = assignment.category_key
              WHERE assignment.symbol = symbol.symbol
                AND assignment.active = TRUE
                AND category_control.strategy_id = %s
                AND category_control.enabled = FALSE
          )
      )
    ORDER BY symbol.symbol
"""
