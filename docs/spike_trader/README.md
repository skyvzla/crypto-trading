# 逼空插针策略文档集

本目录是通用量化交易平台下的**逼空插针策略专项文档集**，不是整个平台的范围定义。

平台的数据层、执行层、账本和回测边界允许未来服务多种策略，因此会比本策略的直接需求更宽泛。但初期只开发逼空插针策略，本目录也是当前实施和验收的主文档集。清算地图、加速识别、多档挂单和退出规则属于该策略，不应自动成为其他策略的通用约束；其他策略同样不进入当前开发范围。平台总体架构以 [当前三层架构](../ARCHITECTURE.md) 为准。

## 主题文档

1. [系统范围与职责](architecture/system.md)
2. [行情数据约束](architecture/data.md)
3. [交易池发现与监听租约](architecture/watch-universe.md)
4. [策略规格与脚本基线](architecture/strategy.md)
5. [交易轮次与持仓生命周期](architecture/campaign.md)
6. [执行、账本与 Web 控制](architecture/execution-ledger-web.md)
7. [回测与验证](architecture/backtest.md)
8. [运行范围](architecture/operations.md)
9. [清算地图（暂空）](architecture/liquidation-map.md)
10. [决策记录](decisions.md)

## 阶段文档

- [阶段总览](phases/README.md)
- [阶段 0：脚本基线](phases/phase-0-baseline.md)
- [阶段 1：策略核心](phases/phase-1-core.md)
- [阶段 2：执行与恢复](phases/phase-2-execution.md)
- [阶段 3：清算地图（暂空）](phases/phase-3-liquidation-map.md)
- [阶段 4：交易池 Web 控制](phases/phase-4-control-plane.md)
- [阶段 5：测试网](phases/phase-5-testnet.md)
- [阶段 6：实盘灰度](phases/phase-6-live.md)

## 文档规则

- [当前三层架构](../ARCHITECTURE.md) 只记录已确认的层级职责和边界。
- 当前脚本事实必须标注为“脚本基线”，不能自动升级为最终策略参数。
- 未确认的实现内容只列为待确认问题，不给出假设答案。
- 清算地图暂不定义接口或字段；没有地图数据不得影响开仓。
- 旧 `src/crypto_trader/` 系统不因本计划直接改变。
