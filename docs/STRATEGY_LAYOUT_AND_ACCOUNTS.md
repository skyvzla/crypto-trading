# 策略目录与交易账户边界

## 目录

按事件类型编排的示例策略仍放在 `strategies/kline/` 和 `strategies/tick/`。
Spike 是一个独立策略子系统，统一放在 `strategies/spike/`：

- `short.py`：核心信号、入场和退出决策
- `exit_features.py`、`exit_policy.py`：候选退出指标和状态机
- `live.py`：订单执行、风险、Campaign 和恢复协调
- `main.py`：测试网/实盘进程入口
- `legacy_research.py`：仅回测研究用的旧退出规则

根目录下的 `spike_*.py` 目前只是兼容旧导入路径，新代码应使用 `strategies.spike.*`。

## 账户

当前运行模型是“一进程一个交易账户”。`STRATEGY_ACCOUNT_ID` 同时用于账本、WAL、执行租约、风险控制和持仓归属；真正的 Binance 账户由该进程使用的 `BINANCE_API_KEY` / `BINANCE_API_SECRET` 决定。

因此，A/B 使用账户 1、C 使用账户 2 的现有做法是拆成两个进程或 Compose service，并分别配置账户 ID 和 Binance 凭证。一个进程内的多个策略实例目前会共享同一个 `StrategyConfig.account_id`、REST 客户端和用户数据流，不支持自动按策略映射到多个账户。

Spike 还要求专用账户（`SPIKE_DEDICATED_STRATEGY_ACCOUNT=true`），不能通过修改 `account_id` 字符串让多个策略安全共享同一个 Binance 账户。若要支持同进程多账户，需要新增按账户隔离的凭证、WAL、执行租约、用户数据流和订单/持仓路由，不能只增加一个映射表。
