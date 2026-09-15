# 策略目录与交易账户边界

## 目录

按事件类型编排的示例策略仍放在 `strategies/kline/` 和 `strategies/tick/`。
Spike 是一个独立策略子系统，统一放在 `strategies/spike/`：

- `short.py`：核心信号、入场和退出决策
- `exit_features.py`、`exit_policy.py`：候选退出指标和状态机
- `live.py`：订单执行、风险、Campaign 和恢复协调
- `main.py`：测试网/实盘进程入口
- `legacy_research.py`：仅回测研究用的旧退出规则

Spike 不保留根目录兼容模块，所有代码统一使用 `strategies.spike.*` 导入路径。

7 天箱体突破做多策略放在 `strategies/long_breakout/`：

- `core.py`：无网络依赖的 4h/1d 信号和持仓状态机
- `execution.py`：BUY 入场、SELL reduce-only 退出及独立执行 worker
- `runtime.py`：账户租约、WAL、User Stream、启动对账、Market 预热和运行状态
- `settings.py`：`LONG_BREAKOUT_*` 环境配置
- `main.py`：独立 testnet 进程入口

两个策略共用方向无关的 `strategies/execution_queue.py`，但不共享队列实例、
worker、账户对象或运行时状态。

## 账户

当前运行模型是“一策略一进程一账户”。`STRATEGY_ACCOUNT_ID` 同时用于账本、
WAL、执行租约、风险控制和持仓归属；真正的 Binance 账户由该进程使用的
`BINANCE_API_KEY` / `BINANCE_API_SECRET` 决定。

因此，A/B 使用账户 1、C 使用账户 2 的现有做法是拆成两个进程或 Compose service，并分别配置账户 ID 和 Binance 凭证。一个进程内的多个策略实例目前会共享同一个 `StrategyConfig.account_id`、REST 客户端和用户数据流，不支持自动按策略映射到多个账户。

Spike 和 long_breakout 都要求专用账户，不能通过修改 `account_id` 字符串让多个
策略安全共享同一个 Binance 账户。逻辑账户 ID、Binance testnet 子账户/凭证和 WAL
路径必须全部不同；当前运行时无法证明两个不同 API key 是否属于同一个 Binance
子账户，因此这一项仍需部署人员核对。

| 边界 | Spike | Long breakout |
| --- | --- | --- |
| Compose service/profile | `spike` | `long_breakout` |
| strategy_id | `spike_short` | `long_breakout` |
| account_id | `SPIKE_ACCOUNT_ID` | `LONG_BREAKOUT_ACCOUNT_ID` |
| Binance 凭证 | `BINANCE_API_KEY/SECRET` | `LONG_BREAKOUT_BINANCE_API_KEY/SECRET` |
| 订单 WAL | `SPIKE_WAL_PATH` | `LONG_BREAKOUT_WAL_PATH` |
| event WAL | `<SPIKE_WAL_PATH>.events.jsonl` | `<LONG_BREAKOUT_WAL_PATH>.events.jsonl` |
| execution worker | `spike-execution-worker` | `long-breakout-execution-worker` |

PostgreSQL account advisory lease 阻止两个进程同时占用同一个逻辑 account_id；严格
启动对账会拒绝不属于本策略的挂单/仓位。若未来需要同一进程承载多个账户，必须新增
按账户隔离的凭证、WAL、租约、User Stream 和订单/持仓路由，不能只增加映射表。
