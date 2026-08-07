# 当前三层架构

## 总览

项目按业务职责划分为三层：

```text
行情数据层
  -> 完成的 1s Bar、已完成 K 线、数据质量与监听池
策略执行层
  -> Spike 核心、Campaign、风控、订单执行、回测适配
账本与 Web 控制层
  -> PostgreSQL 账本、FastAPI、Web 控制、审计与运行状态
```

## 1. 行情数据层

负责 Binance 行情接入、combined stream 解包、aggTrade 聚合为完成的 1s Bar、已完成 K 线存储、Redis 实时分发、历史归档和订阅租约。

策略只能接收完成的 `Bar1s`，不能直接消费 tick。Redis、DuckDB/Parquet 是数据层的基础设施，不构成额外业务层。

## 2. 策略执行层

负责 Spike 策略的统一核心、逐币种 Campaign 运行状态、信号计算、订单意图、账户/币种风控、Binance REST/User Stream、订单幂等、WAL、对账、保护和退出。运行中的 Campaign 以 Redis 原子租约为权威状态，并向账本层发布不可变策略审计事件。

`replay`、`testnet`、`live` 是此层的运行适配模式。回测是离线适配，不另设第四个业务层。执行客户端是库，不通过独立交易代理服务转发。

## 3. 账本与 Web 控制层

负责 PostgreSQL 中的订单、成交、持仓、控制状态、配置和审计记录；通过 FastAPI 提供查询和控制接口；Web 提供交易池 subcategory 控制、账本查看、盈亏和运行状态。Campaign 的持久生命周期从 `strategy_audit_events` 按 `campaign_id` 查询还原，不建立与 Redis 运行态重复的 Campaign 状态表。订单意图在提交前显式携带 `campaign_id`，该身份经 WAL、User Stream 和启动回补进入订单/成交表，用于按 Campaign 聚合实际卖出、买回、手续费及净 PnL。

Web 首版是否包含暂停、撤单、紧急平仓、权限和配置管理，以 `decisions.md` 的后续确认结果为准。

## 边界

- 数据层不决定策略，不下单。
- 策略层不直接依赖 Web 页面，不把提交成功当成交事实。
- 账本与 Web 控制层不替代交易所成交事实，不绕过 Campaign 和风控。
- PostgreSQL Campaign 审计历史不替代 Redis 当前租约；Redis 当前租约也不替代 PostgreSQL 可追溯历史。
- 不按时间窗口推测订单所属 Campaign；缺少显式归属的旧成交保留为空且不进入 Campaign PnL。
- 清算地图当前不属于必要输入，不阻塞 replay、testnet 或 live。
