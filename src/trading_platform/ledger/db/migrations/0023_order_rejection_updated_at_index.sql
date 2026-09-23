DROP INDEX idx_orders_rejected_notifications;

CREATE INDEX idx_orders_rejected_notifications
    ON orders(updated_at, id)
    WHERE status = 'REJECTED';
