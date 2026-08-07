# 项目文档入口

## 当前有效文档

按以下顺序阅读：

1. [Spike 决策记录](spike_trader/decisions.md)：已经确认的业务规则和待确认问题。
2. [当前三层架构](ARCHITECTURE.md)：行情数据层、策略执行层、账本与 Web 控制层的职责边界。
3. [项目完整实施计划](PROJECT_IMPLEMENTATION_PLAN.md)：功能状态、阶段、依赖、验收和风险门禁。
4. [项目功能盘点](PROJECT_GAP_ANALYSIS.md)：当前源码与测试对应的实现快照。
5. [AKEUSDT 2026 年 7 月对齐 Replay](AKEUSDT_2026_07_ALIGNED_REPLAY.md)：当前有效的逐笔结果与已定位问题。

## 文档规则

- `decisions.md` 中“已确认”的内容才是业务实现依据；“待确认”不得猜测落地。
- `PROJECT_IMPLEMENTATION_PLAN.md` 是唯一实施路线，旧阶段文档不得用于排期。
- `PROJECT_GAP_ANALYSIS.md` 是状态快照，代码变化后必须同步更新。
- `docs/archive/` 只用于历史追溯，不作为当前实现、验收或上线依据。

旧策略规格、阶段计划、执行协议、模块设计和完成度声明已经归档。归档文档中的参数、
接口、目录、状态和上线结论均可能过期。
