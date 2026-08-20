ALTER TABLE strategy_capital_events
    ADD COLUMN capital_breached_before BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN capital_breached_after BOOLEAN NOT NULL DEFAULT FALSE;

WITH breach_history AS (
    SELECT
        id,
        COALESCE(
            BOOL_OR(event_type = 'CAPITAL_BREACH') OVER (
                PARTITION BY account_id, strategy_id
                ORDER BY created_at, id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ),
            FALSE
        ) AS breached_before,
        BOOL_OR(event_type = 'CAPITAL_BREACH') OVER (
            PARTITION BY account_id, strategy_id
            ORDER BY created_at, id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS breached_after
    FROM strategy_capital_events
)
UPDATE strategy_capital_events AS events
SET capital_breached_before = history.breached_before,
    capital_breached_after = history.breached_after
FROM breach_history AS history
WHERE history.id = events.id;
