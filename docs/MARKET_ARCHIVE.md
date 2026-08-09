# 历史行情归档

`market-history` 是一次性维护命令，不属于常驻行情进程，也不属于回测执行路径。
下载得到的 Binance Vision 文件按不可变分区写入 Parquet，DuckDB 只生成查询 catalog。
CLI 会显示总文件数、当前文件、平均下载速度、解析状态和写入行数。

不传 `--symbols` 时，CLI 默认从 PostgreSQL `exchange_symbols` 读取当前允许交易的
USD-M 永续币种；也可以显式传 `--all-symbols` 表达相同意图。筛选条件与交易门禁一致：
必须为 active、`PERPETUAL`、`TRADING`、已经上架，且下架时间超出冻结窗口。数据库连接
默认读取 `DB_*` 配置，也可用 `--dsn` 指定。
传入 `--strategy-id` 时还会应用该策略显式关闭的 Category/Subcategory；不传时只应用
交易所生命周期和交易对全局开关。

```bash
uv run market-history data/market/history-parquet \
  --catalog data/market/history.duckdb \
  --symbols AKEUSDT BANKUSDT \
  --timeframes 1s 1m 5m 15m \
  --start 2026-07-01T00:00:00Z \
  --end 2026-08-01T00:00:00Z
```

下载全部可交易币种：

```bash
uv run market-history data/market/history-parquet \
  --all-symbols \
  --timeframes 1s 1m 5m 15m \
  --start 2026-05-01T00:00:00Z \
  --end 2026-08-01T00:00:00Z
```

CLI 默认使用 4 个下载/解析 worker；可用 `--workers 1` 切回串行，或按网络和 CPU
调整。不同分区可以并行写入，同一分区仍由独立锁保护。

如果单个代理有带宽上限，可以重复传入 `--proxy` 配置代理池；每个网络下载请求按轮询顺序
选择一个空闲代理，同一代理在下载和校验完成前不会被再次分配。代理释放后才进入下一轮，
因此 `--workers` 可以大于代理数，超出的 worker 会等待空闲代理。代理 URL 支持 HTTP(S) 和 SOCKS5，
也可以用逗号或换行分隔的 `MARKET_HISTORY_PROXIES` 配置：

```bash
uv run market-history data/market/history-parquet \
  --symbols BTCUSDT ETHUSDT \
  --timeframes 1s 1m \
  --start 2025-01-01T00:00:00Z \
  --end 2026-01-01T00:00:00Z \
  --workers 8 \
  --proxy http://user:pass@proxy-a:8080 \
  --proxy http://user:pass@proxy-b:8080
```

SOCKS5 代理示例：

```bash
--proxy socks5://user:pass@proxy-a:1080
--proxy socks5h://user:pass@proxy-b:1080
```

配置代理时，Binance `exchangeInfo` 元数据请求始终直连，只有历史行情归档下载和校验使用代理池。

不传 `--proxy` 时继续使用 `httpx` 的 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量行为。

磁盘保护默认保留 10 GiB 可用空间，并在启动、每个网络下载前以及每次 Parquet 写入前
检查。可通过 `MARKET_HISTORY_MIN_FREE_GB` 配置，或用 `--min-free-gb` 覆盖；达到阈值
时任务会停止且已完整写入的分区保留。传 `--min-free-gb 0` 可关闭保护。

已存在且可读取的 Parquet 分区默认跳过，不会重复下载；使用 `--overwrite` 可强制重建。
默认输出简洁文本，只有显式传入 `--json` 时才输出完整 JSON 结果。
按 `Ctrl+C` 会正常取消并返回退出码 `130`，不会打印 traceback；已经完整写入的分区保留。
币种上架前尚不存在的官方分区返回 `404` 时会标记为 `unavailable` 并继续，不会中止
整批任务。SSL、超时、连接错误及其他 HTTP 错误会按 `--attempts` 重试，默认 3 次；
重试过程会输出当前次数，耗尽后才失败退出。
如果所选范围全部早于币种上架日期，仍会生成结构完整但没有数据行的 DuckDB catalog。
任务规划前会从 Binance USD-M `exchangeInfo` 读取 `onboardDate` 和 `deliveryDate`，裁掉
上架前及下架后的日/月分区。exchangeInfo 请求失败时会按相同策略重试，耗尽后降级为
不裁剪并继续下载，由 404 规则兜底。

`1s` 对完整自然月使用 Binance Vision monthly `aggTrades`，下载后流式按天按交易时间聚合；
起止边界的残月使用 daily `aggTrades`。其他周期使用原生 monthly Kline。月度 `1s` ZIP
不会整体加载到内存，解析时最多保留当前日的聚合结果。请求范围只决定要下载哪些完整日/月
分区，写入时不会把一个完整分区截断成部分数据。
目录固定为 `SYMBOL/TIMEFRAME/YYYY/MM/DD/candles.parquet`；月度分区最后一层使用
`00` 表示整月。

所有时间输入必须带时区，epoch 毫秒/微秒会先转换为 UTC。Parquet 时间列固定为
`timestamp[ms, tz=UTC]`；每次写分区先写临时文件，再原子替换目标文件。下载文件同时校验
Binance `.CHECKSUM`，任何校验失败都会中止该分区写入。

下载地址：

- 默认官方 S3：`https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data/futures/um/`
- 备用主站：`https://data.binance.vision/data/futures/um/`
- 校验文件：在 ZIP 地址后追加 `.CHECKSUM`

例如 AKEUSDT 的 1s 原始成交文件和 2026-07 的 1m 文件：

```text
https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data/futures/um/daily/aggTrades/AKEUSDT/AKEUSDT-aggTrades-2026-07-01.zip
https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data/futures/um/monthly/aggTrades/AKEUSDT/AKEUSDT-aggTrades-2026-07.zip
https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data/futures/um/monthly/klines/AKEUSDT/1m/AKEUSDT-1m-2026-07.zip
https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data/futures/um/monthly/klines/AKEUSDT/1m/AKEUSDT-1m-2026-07.zip.CHECKSUM
```

误差脚本抽样查询官方 1m Kline 时使用：
`https://fapi.binance.com/fapi/v1/klines`。

S3 出现连接错误时会自动回退主站。`httpx` 默认读取标准代理环境变量，
运行命令前可配置：

```bash
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890
```

验证现有 catalog 或 Parquet 生成的 catalog：

```bash
uv run python scripts/verify_market_history.py data/market/history.duckdb \
  --symbols AKEUSDT BANKUSDT ROBOUSDT 1000RATSUSDT \
  --start 2026-07-01T00:00:00Z \
  --end 2026-08-01T00:00:00Z \
  --official-samples 5
```

回测仍只读 DuckDB catalog，不联网、不写 Parquet。旧的历史 DuckDB 若包含已知偏移数据，
必须显式使用现有回测补偿参数或重新生成 Parquet catalog，不能让新下载模块自动猜测并平移。
