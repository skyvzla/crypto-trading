# 历史文档归档

本目录中的文档保留历史实现声明、旧设计和迁移记录，仅用于追溯，不作为当前实现、验收或上线依据。

## 归档内容

| 目录 | 内容 | 归档原因 |
|---|---|---|
| `root/` | 旧完成总结、状态交接、架构评审、策略架构草案 | 完成度声明互相冲突，部分路径和接口已经过期 |
| `architecture/` | 旧平台总体架构 | 与当前三层名称、实际代码和待确认范围不一致 |
| `implementation/` | 旧实现状态和实现总结 | 同时包含“已完成”和“下一步待做”的冲突声明 |
| `spike_trader/` | 旧 Spike 迁移报告 | 声称迁移完成，但当前 runner 和策略接口不可运行 |
| `modules/` | 旧回测设计和模块说明 | 包含实时纸盘及无未来偏差等未被当前实现证明的声明 |
| `operations/` | 旧部署说明和检查清单 | 将示例策略和假健康状态描述为可部署服务 |
| `specifications/` | 旧 Spike 技术规格 | 参数、目录和退出规则与已冻结脚本基线冲突 |
| `proposals/` | 旧执行与对账协议 | 是目标设计，不代表 WAL、对账和恢复已经实现 |
| `legacy/` | 旧依赖清单 | 不代表当前项目依赖或部署方式 |
| `spike_trader/design-v0/` | 旧专题架构和阶段计划 | 阶段编号与当前唯一实施计划冲突 |

当前依据请阅读：

- `docs/README.md`
- `docs/ARCHITECTURE.md`
- `docs/PROJECT_IMPLEMENTATION_PLAN.md`
- `docs/PROJECT_GAP_ANALYSIS.md`
- `docs/spike_trader/decisions.md`
