# Hermes WebUI Domain Language

Hermes WebUI 的核心领域语言，用于区分 Agent 轮次产生的文件内容变化与 Git 仓库状态变化。

## Language

**File ChangeSet（文件变更集）**:
一轮 Agent 执行产生的权威文件内容变化集合，不包含 Git 仓库状态。
_Avoid_: ChangeSet、变更集

**Git State Transaction（Git 状态事务）**:
一轮 Agent 执行产生的语义 Git 状态变化集合，与 File ChangeSet 分开审计和持久化。
_Avoid_: `.git` 变更集、`.git` 目录回写
