# 项目文档入口

## 当前有效文档

按以下顺序阅读：

1. [Spike 决策记录](spike_trader/decisions.md)：已经确认的业务规则和待确认问题。
2. [当前三层架构](ARCHITECTURE.md)：行情数据层、策略执行层、账本与 Web 控制层的职责边界。
3. [部署与运行手册](DEPLOYMENT.md)：三个运维入口、服务门禁、通知验证和停机边界。
4. [Web 功能与信息架构规划](WEB_PRODUCT_PLAN.md)：已确认的菜单、页面职责、统计口径和实施边界。
5. [项目完整实施计划](PROJECT_IMPLEMENTATION_PLAN.md)：功能状态、阶段、依赖、验收和风险门禁。
6. [项目功能盘点](PROJECT_GAP_ANALYSIS.md)：当前源码与测试对应的实现快照。
7. [AKEUSDT 2026 年 7 月对齐 Replay](AKEUSDT_2026_07_ALIGNED_REPLAY.md)：当前有效的逐笔结果与已定位问题。
8. [Spike 策略版本说明](SPIKE_STRATEGY_VERSIONS.md)：v1、v2 基线与可选实验参数的差异。

## 文档规则

- `decisions.md` 中“已确认”的内容才是业务实现依据；“待确认”不得猜测落地。
- `PROJECT_IMPLEMENTATION_PLAN.md` 是唯一实施路线，旧阶段文档不得用于排期。
- `PROJECT_GAP_ANALYSIS.md` 是状态快照，代码变化后必须同步更新。
- `docs/archive/` 只用于历史追溯，不作为当前实现、验收或上线依据。

旧策略规格、阶段计划、执行协议、模块设计和完成度声明已经归档。归档文档中的参数、
接口、目录、状态和上线结论均可能过期。

运维只使用三个入口：`scripts/deploy.sh` 部署基础服务，`scripts/start.sh [SERVICE]`
启动一个服务，`scripts/stop.sh [SERVICE]` 以 120 秒宽限期停止一个服务。启动/停止脚本
不会替代策略运行时准入、平仓、撤单或备份流程；涉及真实持仓时，先按策略专用停机预案
执行人工风控和交易所对账。

启动门禁使用通知 overview 的 `critical_routes_ready`；`routable_policies` 仅用于诊断，缺失关键事件类型会在脚本输出中列出。
