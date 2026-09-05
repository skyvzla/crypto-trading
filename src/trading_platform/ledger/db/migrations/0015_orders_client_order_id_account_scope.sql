-- Binance client order IDs are unique within an exchange account. The ledger
-- keeps multiple dedicated accounts in one PostgreSQL database, so the
-- account identity must be part of the local idempotency key as well.
ALTER TABLE orders
    DROP CONSTRAINT IF EXISTS orders_client_order_id_key;

ALTER TABLE orders
    ADD CONSTRAINT orders_account_client_order_id_key
        UNIQUE (account_id, client_order_id);
