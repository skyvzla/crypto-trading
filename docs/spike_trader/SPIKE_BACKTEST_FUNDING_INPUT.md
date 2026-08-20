# Spike 动态资金回测的 Funding 输入

动态资金回测必须显式提供一个只读 DuckDB income 快照。回测不会读取
PostgreSQL/Redis，不会联网补数据，也不会写回该快照。

## 输入表

快照复用线上 `account_income_events` 的核心列：

```sql
CREATE TABLE account_income_events (
    account_id VARCHAR,
    transaction_id BIGINT,
    income_type VARCHAR,
    symbol VARCHAR,
    asset VARCHAR,
    amount DECIMAL(30, 12),
    event_time TIMESTAMPTZ
);
```

快照还必须记录已经完整导出的时间窗，避免缺数据被静默解释为资金费为 0：

```sql
CREATE TABLE account_income_coverage (
    account_id VARCHAR,
    income_type VARCHAR,
    symbol VARCHAR,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ
);
```

`account_income_coverage` 使用 `[start_time, end_time)`；多个首尾相接或重叠区间
可以共同覆盖回测窗口。完整覆盖且 `account_income_events` 没有匹配行，才表示该窗口
资金费明确为 0。

只读取 `income_type = 'FUNDING_FEE'`、指定账户和交易对的 USDT 事实。
`amount` 沿用 Binance income 的符号：收入为正，支出为负。同一
`transaction_id` 的相同事实会去重，冲突事实会拒绝回测。

## Campaign 归属

资金费按交易对归入从首次入场成交到最终退出成交的闭区间。边界事实会计入当前
Campaign，但一个 transaction ID 最多消费一次。部分退出不会提前触发资金结算。

## CLI

动态资金参数之外还需提供：

```text
--funding-duckdb-path /path/to/funding.duckdb
--funding-account-id spike-account
```

固定资金/旧三档回测不需要这两个参数；给固定资金模式传入 funding 参数会被拒绝，
避免产生“已计入资金费”的错误理解。
