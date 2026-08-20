# 振幅周期指标计算方案

## 目标

脚本 `tools/compute_amplitude_cycle_indicators.py` 针对以下事件文件计算做空短线逼空后的反转/衰竭指标：

- `reports/amplitude/daily_amplitude_cycles_spike_up.csv`
- `reports/amplitude/daily_amplitude_cycles_spike_down.csv`

事件中的 `start_utc`、`end_utc`、`high_utc`、`low_utc` 会被转换为毫秒时间戳，并保留原始字段、来源文件、来源行号和稳定 `event_id`。

## 数据读取

行情读取遵循回测引擎的数据路径：先加载 `archive_index.parquet`，再用 DuckDB 的 `read_parquet(...)` 只读查询实际覆盖事件区间的 Parquet 文件。不会读 Redis、联网补数据或回写归档。

metrics sidecar 使用 `metrics_index.parquet` 选择文件，并以 `available_ms` 做 backward `merge_asof`。因此某根 K 线只能看到该 K 线结束前已经可用的 metrics 快照，不会把未来快照带入历史指标。

默认启用 archive 和 metrics 索引文件校验；仅在明确需要烟测或索引文件尚未齐全时使用 `--skip-index-verify`。

## 周期与窗口

输出周期为：

- 原生行情：`1s`、`1m`、`5m`、`15m`、`1h`
- 聚合行情：由原生 `1s` 聚合得到 `5s`

计算时会向前扩展 warmup，向后扩展目标区间：

| 数据 | 前置窗口 | 后置窗口 | 用途 |
| --- | ---: | ---: | --- |
| 分钟及以上 K 线 | 72 小时 | 30 分钟 | EMA、ADX、波动率、箱体和背景趋势 |
| metrics | 5 小时 | 30 分钟 | OI、持仓比和其他衍生快照 |
| `1s`/`5s` 微观 K 线 | 60 秒 | 30 分钟 | spike 年龄、创新高速度、时间分布和衰竭触发 |

同一 symbol 的多个事件会先合并区间再读取，避免按每个事件重复扫描；持久化时只保留事件前后分析所需数据，warmup 行仅用于计算。

## 指标范围

`feature_dictionary.parquet` 是指标词典，记录每个指标的周期、角色、参数、数据源、公式版本和状态。当前实现覆盖：

- 趋势/位置：EMA、ROC、RSI、MACD、ADX/DI、趋势斜率、斜率衰减、箱体、VWAP 偏离、距近期/日内高点距离
- 波动/结构：布林带宽度、ATR、实现波动率、波动率分位、Choppiness、效率比、失败突破、回踩失败、CHOCH、BOS、lower high、上影线和收盘位置
- 量价衰竭：成交量倍数、z-score、OBV、MFI、上下行量比、量能变异系数、量能峰值后衰减、连续阳线、创新高计数、TD sell setup
- 事件锚点：`start`、`end`、`high`、`low` 四类快照，并保存对应周期的最后一根可见 K 线和事件级指标
- 衍生数据：OI、OI value、OI delta、OI acceleration、价格/OI 象限、long-short、top-trader long-short、taker long-short volume ratio
- 结果标签：结束后的 30s、1m、3m、5m、10m、15m、30m 收益、前向最高/最低、MAE/MFE 及发生时间

没有逐笔成交、主动成交方向、盘口、强平、现货/永续价差或逐价位成交量分布归档时，不用 OHLCV 方向伪造这些指标。它们会在指标词典中标记为 `unsupported_source`；metrics 索引不存在时，metrics 指标标记为 `missing_source`。

## 输出结构

默认输出到 `reports/amplitude/indicator_features/run_id=<UTC 时间>/`，也可通过 `--out-dir` 指定：

```text
events.parquet                         原始事件与强类型时间戳
feature_dictionary.parquet              指标定义、周期、参数、状态
event_features/part-<symbol>.parquet   四类事件锚点的宽表
bars/{1s,5s,1m,5m,15m,1h}/             逐周期 OHLCV 和指标列
derivatives/part-<symbol>.parquet       原始 metrics sidecar 快照
availability/part-<symbol>.parquet      每事件/数据源/周期覆盖情况
targets/part-<symbol>.parquet           事件结束后的前向结果
failures/part-<symbol>.parquet          单 symbol 失败记录
manifest.json                           输入摘要、索引、worker 和行数
```

Parquet 使用 zstd 压缩，适合 DuckDB、Polars、Pandas 和 PyArrow 直接查询。逐周期数据按 symbol 分片，便于并行分析和增量删除单个 symbol 的结果。

## 运行

单事件烟测：

```bash
uv run --frozen python tools/compute_amplitude_cycle_indicators.py \
  --limit-events 1 \
  --workers 1 \
  --duckdb-threads 1 \
  --out-dir /tmp/amplitude-indicator-smoke \
  --skip-index-verify
```

多 worker 一致性或小批量运行：

```bash
uv run --frozen python tools/compute_amplitude_cycle_indicators.py \
  --limit-events 2 \
  --workers 2 \
  --duckdb-threads 1 \
  --out-dir /tmp/amplitude-indicator-w2
```

全量运行（默认 13 个 worker）：

```bash
uv run --frozen python tools/compute_amplitude_cycle_indicators.py \
  --workers 13 \
  --duckdb-threads 1 \
  --out-dir reports/amplitude/indicator_features/run_id=$(date -u +%Y%m%dT%H%M%SZ)
```

`workers` 是 symbol 级并发；每个 worker 使用独立的内存 DuckDB 连接。`duckdb-threads=1` 可避免进程级并发与连接内线程过度竞争，机器资源充足时再单独调大。

## 查询示例

查看所有事件锚点的 1 分钟触发指标：

```sql
SELECT event_id, symbol, direction, anchor_role, anchor_ms,
       "1m_failed_breakout", "1m_breakout_retest_failure",
       "1m_micro_CHOCH", "1m_lower_high", "1m_volume_climax_then_decay",
       event_open_interest, event_oi_price_quadrant
FROM read_parquet('event_features/*.parquet')
ORDER BY anchor_ms;
```

检查数据覆盖和缺口：

```sql
SELECT source, timeframe, status, count(*) AS events,
       sum(gap_count) AS gaps
FROM read_parquet('availability/*.parquet')
GROUP BY source, timeframe, status
ORDER BY source, timeframe, status;
```

按事件方向分析前向结果：

```sql
SELECT e.direction,
       avg(t.ret_after_1m) AS avg_ret_1m,
       quantile_cont(t.ret_after_1m, 0.5) AS median_ret_1m,
       avg(t.fwd_max_5m) AS avg_fwd_max_5m,
       avg(t.fwd_min_5m) AS avg_fwd_min_5m
FROM read_parquet('events.parquet') e
JOIN read_parquet('targets/*.parquet') t USING (event_id)
GROUP BY e.direction;
```

## 分析注意事项

1. 事件锚点只读取 `close_ms + 1 <= anchor_ms` 的完整 K 线；未闭合或发生在锚点之后的 K 线不会进入快照。
2. metrics 只允许 as-of 到 `available_ms`，不能按 `snapshot_ms` 直接回填。
3. `unsupported_source` 与 `missing_source` 是数据可用性结论，不应在后续分析中当作零值。
4. 多 worker 只改变计算分片和完成顺序，不改变结果；比较结果时使用 `event_id, anchor_role, anchor_ms` 排序。
5. `targets` 是描述性标签，不参与事件锚点指标计算，避免把未来收益泄漏到触发特征。
