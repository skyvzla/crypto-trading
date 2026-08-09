# 交易对数据与准入

交易对事实、交易所分类和准入开关统一存放在 PostgreSQL。策略和历史下载器只消费
数据层计算出的有效交易池，不直接从 Binance 临时补齐或维护另一份开关。

## 同步

手动同步生产 Binance USD-M `exchangeInfo`：

```bash
uv run exchange-symbol-sync
```

本地宿主机运行 CLI 前先启动 PostgreSQL：

```bash
docker compose up -d postgres
uv run exchange-symbol-sync
```

Compose 将 PostgreSQL 映射到 `localhost:5432`；容器内服务仍使用网络名 `postgres`。

可用 `--dsn` 指定 PostgreSQL，`--attempts` 和 `--timeout` 控制网络请求。默认输出简洁
摘要，只有传入 `--json` 才输出 JSON。同步固定访问 `https://fapi.binance.com`，不受
testnet 执行配置影响。

Compose 的 `symbol-sync` 服务启动后立即同步一次，之后默认每 86400 秒同步。间隔通过
`EXCHANGE_SYMBOL_SYNC_INTERVAL_SECONDS` 配置。网络或数据库失败会保留上一份成功快照，
记录失败状态并在最多 5 分钟后重试，不会把交易对批量标记为 inactive。失败状态下有效
交易池查询会关闭，直到下一次完整同步成功。

同步内容包括交易对生命周期、资产、合约字段、原始 metadata，以及 Binance
`underlyingType` 和 `underlyingSubType` 两级分类。同步只更新 Binance 事实与分类关联，
并严格按官方 `quoteAsset=USDT` 过滤；USDC、USD1、BTC、U 等其他计价资产不会写入交易对表、分类关联或有效交易池。
不会覆盖人工维护的全局交易对开关或策略分类开关。

首次同步还会写入缺省准入规则：当前市值最大的基准资产（BTC、ETH、BNB、XRP、SOL
等 32 个 Binance USD-M 永续）默认全局禁用，避免策略进入主流币；已有人工开关绝不
会被覆盖。旧 `subcategory_admission` 中的禁用记录只在能唯一匹配 Binance 当前分类时
迁移到 `spike_short` 的分类开关，歧义分类和测试残留不会迁移。

Spike 不请求或写入这份元数据；每次安全扫描只读取 PostgreSQL 的有效交易池。执行连接
仍会独立读取 `exchangeInfo` 中的 tick/step/min-notional 规则，并按 24 小时刷新。

## 准入顺序

新开仓依次执行：

1. 交易所 active、`PERPETUAL`、`TRADING`、已上架且未进入退市冻结窗口。
2. `symbol_global_admission` 全局交易对开关；缺省为允许。
3. `strategy_category_admission` 中该策略匹配分类的显式关闭规则。

策略分类配置是可选过滤器。策略没有任何配置、分类没有配置或币种没有分类时均不受影响；
只有匹配记录 `enabled=false` 才阻止该策略开仓。交易对同时关联父 Category 和所有
Subcategory，因此关闭父分类会覆盖全部子分类；任意匹配分类关闭都优先阻止。

管理 API：

- `GET /api/v1/exchange-symbols`
- `GET /api/v1/exchange-categories`
- `GET|PUT /api/v1/exchange-symbols/{symbol}/admission`
- `GET|PUT /api/v1/strategy-category-admissions/...`
- `GET /api/v1/symbol-global-admission-audit`
- `GET /api/v1/strategy-category-admission-audit`
