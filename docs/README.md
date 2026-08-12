# 文档导航

本目录按文档用途组织；根目录只保留项目入口和贡献约束。

| 目录 | 内容 |
| --- | --- |
| [`architecture/`](architecture/) | 系统架构、Agent 消息语义与 `state.db` 数据模型。 |
| [`api/`](api/) | WebUI、会话、MCP、Cron 与技能相关的 HTTP 接口说明。 |
| [`guides/`](guides/) | 部署、远程访问、工作区 Git 与高级聊天配置指南。 |
| [`integration/`](integration/) | WebUI 与外部服务、SkillHub 的接口说明和约束。 |
| [`operations/`](operations/) | 流式诊断日志。 |
| [`plans/`](plans/) | 尚未实施或分阶段实施的设计方案。 |
| [`fix/`](fix/) | 已定位问题的修复方案。 |
| [`rfcs/`](rfcs/) | 需要长期维护的架构与持久化契约。 |
| [`ui-ux/`](ui-ux/) | UI/UX 设计提案和视觉证据。 |
| `pr-media/` / `pr-assets/` | 本地 Pull Request 审查证据，不纳入 Git 跟踪。 |

## 根目录入口

- [`CONTRACTS.md`](CONTRACTS.md) - 项目契约与 RFC 索引。
- [`GUIDELINES.md`](GUIDELINES.md) - 贡献与变更准则。
- [`UIUX-GUIDE.md`](UIUX-GUIDE.md) - UI/UX 设计准则。
- [`EXTENSIONS.md`](EXTENSIONS.md) - WebUI 扩展机制。
- [`ISSUES.md`](ISSUES.md) - 上游问题根因分析。
- [`onboarding.md`](onboarding.md) - 首次运行引导。
- [`onboarding-agent-checklist.md`](onboarding-agent-checklist.md) - Agent 协助安装的安全清单。

## 中文文档

- [`architecture/HermesAgent消息补充说明文档.md`](architecture/HermesAgent消息补充说明文档.md) - Agent 运行时消息的语义与 WebUI 展示边界。
- [`architecture/HermesAgent上下文压缩逻辑说明.md`](architecture/HermesAgent上下文压缩逻辑说明.md) - Agent 侧上下文压缩的触发、摘要、会话持久化与故障分支。
- [`architecture/state_db表结构说明.md`](architecture/state_db表结构说明.md) - Hermes Agent `state.db` 的 schema 和字段说明。
- [`integration/skillhub后端接口文档约束.md`](integration/skillhub后端接口文档约束.md) - SkillHub 对外接口约束。
- [`integration/webui-external-service-接口说明.md`](integration/webui-external-service-接口说明.md) - WebUI 与外部服务的调用关系。
- [`integration/integration-common-tasks-api.md`](integration/integration-common-tasks-api.md) - 欢迎页常办任务接口。
- [`operations/日志说明文档.md`](operations/日志说明文档.md) - 聊天流 `stream_diag` 日志说明。
- [`plans/轮次级文件事务与归档方案.md`](plans/轮次级文件事务与归档方案.md) - 轮次级文件 ChangeSet 与归档设计。
- [`fix/异步wakeup原始用户重放去重修复方案.md`](fix/异步wakeup原始用户重放去重修复方案.md) - 异步 Wakeup 重放去重方案。
