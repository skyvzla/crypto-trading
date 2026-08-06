# 账本层 (Ledger Layer)

账本层是量化交易平台的数据中心，负责存储和查询所有交易流水、持仓状态、紧急控制和策略配置。

## 架构设计

- **数据库**: PostgreSQL（事务原子性、索引优化）
- **后端**: FastAPI + psycopg3 异步接口
- **通信**: 策略进程直接写入 PostgreSQL，Web 通过 HTTP API 查询

## 目录结构

```
ledger/
├── db/
│   ├── schema.sql       # 数据库表结构（6张表）
│   └── models.py        # psycopg3 数据模型和 CRUD 方法
├── api/
│   └── routes.py        # FastAPI 查询接口
└── main.py              # 账本层主进程
```

## 核心表结构

### 1. orders - 订单表
- 主键：id (BIGSERIAL)
- 唯一键：(account_id, symbol, order_id)
- 索引：strategy_id, symbol, status, created_at
- 字段：订单状态、成交数量、均价、手续费等

### 2. trades - 成交流水表
- 主键：id (BIGSERIAL)
- 唯一键：(account_id, symbol, trade_id) - 防止重复处理
- 索引：strategy_id, symbol, order_id, created_at
- 字段：成交价格、数量、手续费、已实现盈亏等

### 3. positions - 持仓表
- 主键：id (BIGSERIAL)
- 唯一键：(account_id, strategy_id, symbol, position_side)
- 字段：持仓数量、开仓均价、未实现盈亏、强平价格等

### 4. account_control_state - 账户控制状态表
- 主键：account_id
- 字段：desired_state, state_version（版本号）
- 状态：NORMAL, HALT_NEW, CANCEL_ORDERS, CLOSE_ALL

### 5. control_command_log - 控制命令审计日志
- 主键：id (BIGSERIAL)
- 只追加，记录命令执行历史

### 6. strategy_config - 策略配置表
- 主键：id (SERIAL)
- 唯一键：(account_id, strategy_id, config_key)

## API 接口

### 查询接口

- `GET /api/v1/orders` - 订单列表（可按策略/账户/币种筛选）
- `GET /api/v1/trades` - 成交流水
- `GET /api/v1/positions` - 当前持仓
- `GET /api/v1/pnl` - 盈亏统计（已实现+未实现，胜率，平均盈亏）

### 紧急控制

- `GET /api/v1/account_control_state/{account_id}` - 获取控制状态
- `PUT /api/v1/account_control_state/{account_id}` - 更新控制状态
  - 原子递增 state_version
  - 同时写入 control_command_log 审计日志

### 配置管理

- `GET /api/v1/config/{account_id}/{strategy_id}` - 获取策略配置
- `PUT /api/v1/config/{account_id}/{strategy_id}/{config_key}` - 更新配置

### 健康检查

- `GET /api/v1/health` - 服务健康状态

## 紧急控制机制

采用双表设计，解决重启失效和乱序问题：

1. **account_control_state** - 当前期望状态（每账户一行）
   - 包含 state_version 版本号
   - 策略进程每秒轮询，检测版本号变化

2. **control_command_log** - 审计日志（只追加）
   - 记录命令发起者、执行时间、执行结果

### 策略进程处理流程

1. **启动时**：读取 account_control_state，进入对应状态
2. **运行中**：每秒轮询，检查 state_version 是否增加
3. **检测到变化**：执行新状态操作，回写执行结果

## 启动方式

### 1. 初始化数据库

```bash
docker compose -f compose.yaml up -d postgres
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

### 3. 启动账本层服务

```bash
docker compose -f compose.yaml up -d --build ledger
```

### 4. 访问 API 文档

启动后访问：http://localhost:8001/docs

## 事务保证

所有写入操作都在事务内执行：

- `insert_order` - 使用 ON CONFLICT 幂等 upsert
- `insert_trade` - 使用 ON CONFLICT DO NOTHING 防止重复
- `upsert_position` - 原子更新持仓
- `update_account_control_state` - 原子递增版本号 + 写审计日志

## 索引优化

所有查询高频字段都有索引：

- 单列索引：strategy_id, symbol, status, created_at
- 复合索引：(account_id, symbol) 用于快速查询特定账户+交易对
- 唯一索引：防止重复订单和成交

## 依赖

- psycopg (v3) + psycopg-pool - PostgreSQL 异步客户端
- FastAPI + uvicorn - Web 框架
- pydantic + pydantic-settings - 数据验证和配置管理

## 与其他层的关系

- **策略层** → 直接写入 PostgreSQL（订单、成交、持仓）
- **Web 前端** → HTTP API 查询
- **行情层** → 无直接交互

## 设计特性

1. **类型注解完整** - 所有函数都有类型提示
2. **异步原子事务** - psycopg3 异步上下文管理器
3. **连接池管理** - AsyncConnectionPool，最小2连接，最大10连接
4. **幂等写入** - 使用 ON CONFLICT 防止重复
5. **紧急控制版本化** - state_version 解决乱序和重启失效
6. **审计日志** - 所有控制命令都有可追溯记录

## V1 限制

- 配置不支持热更新（需重启策略进程）
- 紧急停止无 ack 确认机制（V2 扩展）
- 单策略独占交易对（V2 支持多策略共享）
