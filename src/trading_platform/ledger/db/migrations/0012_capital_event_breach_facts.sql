ALTER TABLE strategy_capital_events
    ADD COLUMN capital_breached_before BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN capital_breached_after BOOLEAN NOT NULL DEFAULT FALSE;
