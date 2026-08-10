ALTER TABLE backtest_runs
    ADD CONSTRAINT backtest_runs_run_id_not_blank
    CHECK (btrim(run_id) <> '');

ALTER TABLE backtest_trades
    ADD CONSTRAINT backtest_trades_trade_id_not_blank
    CHECK (btrim(trade_id) <> '');

ALTER TABLE backtest_orders
    ADD CONSTRAINT backtest_orders_order_id_not_blank
    CHECK (btrim(order_id) <> '');

ALTER TABLE backtest_fills
    ADD CONSTRAINT backtest_fills_fill_id_not_blank
    CHECK (btrim(fill_id) <> '');

ALTER TABLE backtest_fills
    ADD CONSTRAINT backtest_fills_order_fk
    FOREIGN KEY (research_id, run_id, order_id)
    REFERENCES backtest_orders(research_id, run_id, order_id)
    ON DELETE CASCADE;
