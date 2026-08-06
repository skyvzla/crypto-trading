# 项目文档入口

## 当前有效文档

- [项目实施计划](PROJECT_IMPLEMENTATION_PLAN.md)：唯一的实施路线、阶段目标、验收条件和风险门禁。
- [三层架构](ARCHITECTURE.md)：行情数据层、策略执行层、账本控制层的职责与边界。
- [项目功能盘点](PROJECT_GAP_ANALYSIS.md)：当前源码和测试得到的已完成项、缺失项与 P0/P1 问题。
- [Spike 策略规格](../SPIKE_STRATEGY_SPEC.md)：策略规则草案；未确认参数不得直接实现。
- [Spike 决策记录](spike_trader/decisions.md)：已确认决策和待确认问题。
- [阶段总览](spike_trader/phases/README.md)：阶段依赖与退出条件。

## 文档规则

1. `PROJECT_IMPLEMENTATION_PLAN.md` 规定当前工作顺序。
2. `spike_trader/decisions.md` 中的“已确认”才可以作为实现约束；“待确认”不得猜测落地。
3. `docs/archive/` 只保存历史材料，不作为当前验收依据。
4. 研究报告是实验事实，不等于生产策略结果。
