-- 账本层数据库表结构
-- 版本：v1.0
-- 创建日期：2026-08-06

-- 订单表
CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(32) NOT NULL,
    strategy_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    order_id VARCHAR(64) NOT NULL,  -- Binance orderId
    client_order_id VARCHAR(64) NOT NULL UNIQUE,  -- 客户端订单ID，全局唯一
    side VARCHAR(16) NOT NULL,  -- BUY, SELL
    order_type VARCHAR(16) NOT NULL,  -- LIMIT, MARKET, STOP_MARKET, TAKE_PROFIT_MARKET
    position_side VARCHAR(16),  -- LONG, SHORT, BOTH
    quantity DECIMAL(20, 8) NOT NULL,
    price DECIMAL(20, 8),  -- 限价单价格，市价单为 NULL
    stop_price DECIMAL(20, 8),  -- 止损/止盈触发价格
    status VARCHAR(32) NOT NULL,  -- NEW, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED, EXPIRED
    filled_quantity DECIMAL(20, 8) NOT NULL DEFAULT 0,
    avg_fill_price DECIMAL(20, 8),
    commission DECIMAL(20, 8),
    commission_asset VARCHAR(16),
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    exchange_created_at TIMESTAMP,  -- Binance 服务器时间
    filled_at TIMESTAMP,  -- 完全成交时间

    -- 索引优化
    CONSTRAINT orders_account_symbol_order_id_key UNIQUE (account_id, symbol, order_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_strategy_id ON orders(strategy_id);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_account_symbol ON orders(account_id, symbol);


-- 成交流水表（唯一键：account_id, symbol, trade_id）
CREATE TABLE IF NOT EXISTS trades (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(32) NOT NULL,
    strategy_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    trade_id VARCHAR(64) NOT NULL,  -- Binance tradeId
    order_id VARCHAR(64) NOT NULL,  -- 关联的 Binance orderId
    client_order_id VARCHAR(64) NOT NULL,
    side VARCHAR(16) NOT NULL,  -- BUY, SELL
    position_side VARCHAR(16),  -- LONG, SHORT, BOTH
    quantity DECIMAL(20, 8) NOT NULL,
    price DECIMAL(20, 8) NOT NULL,
    quote_quantity DECIMAL(20, 8) NOT NULL,  -- 交易额（价格 × 数量）
    commission DECIMAL(20, 8) NOT NULL,
    commission_asset VARCHAR(16) NOT NULL,
    realized_pnl DECIMAL(20, 8),  -- 已实现盈亏
    is_maker BOOLEAN NOT NULL,  -- 是否 Maker
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    exchange_time TIMESTAMP NOT NULL,  -- Binance 成交时间

    -- 唯一约束：防止重复处理同一笔成交
    CONSTRAINT trades_account_symbol_trade_id_key UNIQUE (account_id, symbol, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_trades_strategy_id ON trades(strategy_id);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id);
CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_account_symbol ON trades(account_id, symbol);


-- 持仓快照表
CREATE TABLE IF NOT EXISTS positions (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(32) NOT NULL,
    strategy_id VARCHAR(64) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    position_side VARCHAR(16) NOT NULL,  -- LONG, SHORT, BOTH
    quantity DECIMAL(20, 8) NOT NULL,  -- 持仓数量（正数）
    entry_price DECIMAL(20, 8) NOT NULL,  -- 开仓均价
    mark_price DECIMAL(20, 8),  -- 标记价格
    unrealized_pnl DECIMAL(20, 8),  -- 未实现盈亏
    liquidation_price DECIMAL(20, 8),  -- 强平价格
    leverage INTEGER,  -- 杠杆倍数
    margin_type VARCHAR(16),  -- isolated, cross
    isolated_margin DECIMAL(20, 8),  -- 逐仓保证金
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),

    -- 唯一约束：每个账户+策略+交易对+仓位方向只有一个持仓记录
    CONSTRAINT positions_account_strategy_symbol_side_key UNIQUE (account_id, strategy_id, symbol, position_side)
);

CREATE INDEX IF NOT EXISTS idx_positions_account_id ON positions(account_id);
CREATE INDEX IF NOT EXISTS idx_positions_strategy_id ON positions(strategy_id);
CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol);
CREATE INDEX IF NOT EXISTS idx_positions_updated_at ON positions(updated_at DESC);


-- 账户紧急控制状态表（每账户一行）
CREATE TABLE IF NOT EXISTS account_control_state (
    account_id VARCHAR(32) PRIMARY KEY,
    desired_state VARCHAR(32) NOT NULL,  -- NORMAL, HALT_NEW, CANCEL_ORDERS, CLOSE_ALL
    state_version BIGINT NOT NULL DEFAULT 1,  -- 版本号，每次状态变更递增
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(64),  -- 操作者（Web 用户、管理员等）
    reason TEXT  -- 变更原因
);

CREATE INDEX IF NOT EXISTS idx_account_control_state_updated_at ON account_control_state(updated_at DESC);


-- 控制命令审计日志表（只追加，不修改）
CREATE TABLE IF NOT EXISTS control_command_log (
    id BIGSERIAL PRIMARY KEY,
    account_id VARCHAR(32) NOT NULL,
    command VARCHAR(32) NOT NULL,  -- NORMAL, HALT_NEW, CANCEL_ORDERS, CLOSE_ALL
    issued_by VARCHAR(64),  -- 发起者
    issued_at TIMESTAMP NOT NULL DEFAULT NOW(),
    executed_at TIMESTAMP,  -- 策略进程执行完成时间
    execution_result TEXT,  -- 执行结果（JSON 格式，包含撤单数、平仓数等）
    execution_error TEXT  -- 执行失败时的错误信息
);

CREATE INDEX IF NOT EXISTS idx_control_command_log_account_id ON control_command_log(account_id);
CREATE INDEX IF NOT EXISTS idx_control_command_log_issued_at ON control_command_log(issued_at DESC);


-- 策略配置表
CREATE TABLE IF NOT EXISTS strategy_config (
    id SERIAL PRIMARY KEY,
    account_id VARCHAR(32) NOT NULL,
    strategy_id VARCHAR(64) NOT NULL,
    config_key VARCHAR(128) NOT NULL,
    config_value TEXT NOT NULL,  -- JSON 格式存储配置值
    config_type VARCHAR(32) NOT NULL,  -- string, int, float, bool, json
    description TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_by VARCHAR(64),

    -- 唯一约束：每个策略的每个配置键只有一条记录
    CONSTRAINT strategy_config_account_strategy_key_unique UNIQUE (account_id, strategy_id, config_key)
);

CREATE INDEX IF NOT EXISTS idx_strategy_config_account_id ON strategy_config(account_id);
CREATE INDEX IF NOT EXISTS idx_strategy_config_strategy_id ON strategy_config(strategy_id);
CREATE INDEX IF NOT EXISTS idx_strategy_config_updated_at ON strategy_config(updated_at DESC);


-- 初始化默认控制状态（如果表为空）
INSERT INTO account_control_state (account_id, desired_state, state_version, updated_by, reason)
VALUES
    ('account_a', 'NORMAL', 1, 'system', '初始化默认状态'),
    ('account_b', 'NORMAL', 1, 'system', '初始化默认状态')
ON CONFLICT (account_id) DO NOTHING;
