# v3 / v21 / v22 研究结论

## 结论摘要

- `pullback-v3` 保持严格核心参数。全量基线只有 15 笔成交、14 胜，净利约 `3265.8U`；放宽 60 秒涨幅、回撤比例、涨幅窗口或等待时间均未形成可接受的收益/质量改善。下一步优先审计 timeout 漏斗，而不是扩大覆盖。
- `v21` 的收益优先候选为 25 分钟持有期，风险优先候选为 20 分钟；reject-age 是否改善尾部风险以源码冻结后的 smoke 结果为准，不能使用被中断的旧指纹。
- `v22` 的风险收益首选为 20 分钟，约保留最高收益的 98.3%，并显著低于 15/25 分钟的已实现回撤。

## 已证伪的退出实验

1 秒 close 软件 hard stop（8%/15%）显著损害收益，且没有降低组合最大回撤；它也不是交易所条件单意义上的硬止损。因此 hard/gate stop 只保留为离线研究参数，未授权接入 live。

组合 drawdown halt 实验已移除。原实现以累计 PnL 的正峰值为分母，既不是本金净值回撤，也不能代表跨币种组合风险，不能用于回答组合 MDD。

## 因子 OOS

因子 CLI 使用训练期拟合的 Q1/QN 边界，并在测试期原样应用；训练集 purge 至少覆盖目标标签 horizon（例如 `short_mfe_30m` 为 30 分钟），另加用户 embargo。完整事件 dataset 可单独落盘，但 discovery 表只使用 purge 后训练集。

因子边界两端与多因子组合属于探索性多重比较，OOS 表不能直接作为上线授权；必须在后续未查看的 untouched holdout 上确认，并同步检查覆盖率、MFE、MAE 与交易成本。

冻结源码后的 `spike-v21-reject-age-smoke`（12 币、48 runs、2 workers）显示：20 分钟 + reject 的净利 `2066.0U`、胜率 `66.0%`，25 分钟 + reject 净利 `2090.5U`、胜率 `64.6%`；对应不 reject 均为负净利（约 `-695.9U` / `-161.0U`）。这是尾部 smoke 而非全量确认，因此只保留 reject 作为后续候选，不直接扩大到 489 币。

因子读取默认 24 小时 chunk、DuckDB 单线程和 512MB 上限，并验证所选物理归档文件的 size/mtime 与索引一致。线上策略不下载历史、不运行归档器；回测只读 DuckDB/DuckDB 指向的 Parquet 归档。

## 安全状态机

candidate full exit（time/momentum/trend/profit drawdown/hard/gate/OI）共享退出 latch。触发后先撤销本 campaign 的非 reduce-only 入场单，等待撤单和持仓更新，再提交一次退出意图；连续 bar 不重复下单。hard stop 即使候选特征暂缺也可触发，OI stop 默认关闭且仅用于离线研究。
