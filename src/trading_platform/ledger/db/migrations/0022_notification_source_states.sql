CREATE TABLE notification_source_states (
    state_key VARCHAR(256) PRIMARY KEY CHECK (btrim(state_key) <> ''),
    state TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO notification_source_states (state_key, state)
VALUES (
    'notification-order-failure-start',
    (NOW() - INTERVAL '5 minutes')::TEXT
)
ON CONFLICT (state_key) DO NOTHING;

CREATE INDEX idx_execution_event_journal_order_failures
    ON execution_event_journal(event_time, id)
    WHERE event_type = 'execution.order_submit_failed'
       OR (event_type = 'execution.order_submit_result'
           AND details->>'status' IN ('REJECTED', 'SUBMIT_UNKNOWN'));

CREATE INDEX idx_orders_rejected_notifications
    ON orders(created_at, id)
    WHERE status = 'REJECTED';
