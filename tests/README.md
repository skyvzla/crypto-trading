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
| `scripts/` | 运维与 testnet 验证脚本自身的测试 |

同一规则的一组正常、边界和失败案例保留在一个测试文件中；只有形成独立组件或运行
场景时才拆新文件。
