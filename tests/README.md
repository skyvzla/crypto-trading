# 测试目录

测试按业务边界归档，新增测试应放到对应目录，避免再次平铺到 `tests/` 根目录。

| 目录 | 范围 |
|---|---|
| `backtest/` | 回测引擎、数据加载、CLI 与历史 replay |
| `market/` | 行情接入、质量、Redis 和交易池扫描 |
| `strategies/` | 策略信号、准入、live 协调与退出决策 |
| `shared/binance/` | Binance REST/User Stream、执行、恢复和规则量化 |
| `shared/execution/` | 通用账户协议、WAL 与 Campaign 基础设施 |
| `ledger/` | PostgreSQL 账本、回报适配和 Web API |
| `integration/` | 跨层服务组合测试 |
| `research/` | 指标研究、参数标定和研究报告生成逻辑 |
| `scripts/` | 运维与 testnet 验证脚本自身的测试 |

默认按被测模块建立测试文件，例如 `test_live_executor.py`。当某条规则拥有较多独立的
正常、边界和失败案例，或需要专属 fixture 时，可以单独建立规则测试文件；文件名应表达
业务规则，例如 `test_spike_order_ids.py`。不要仅为减少单个文件行数拆分测试。
