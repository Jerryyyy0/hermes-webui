# Cron Skill 提示压缩方案

- **Status:** Proposed
- **Author:** Hermes WebUI maintainers
- **Created:** 2026-09-03
- **Related:** [Cron Execution Identity 与 Artifact 结算修复](cron-session-manifest-artifact-settlement.md)

## 决策摘要

Cron 不再在首条 user message 内联完整 Skill 正文。改造只发生在现有
`cron/scheduler.py` 的 Cron prompt 组装与 `AIAgent(...)` 构造接缝：

1. 不向 user message 注入旧的第 1--4 层；
2. 保留第 5 层，即任务 prompt、runtime token 展开、script stdout/error 与
   `context_from` 输出；
3. 在第 5 层末尾追加一行紧凑的中文 Skill 名称提示；
4. Cron delivery / `[SILENT]` 规则与已有 workspace 说明一起放入本轮的
   ephemeral system context；
5. 不强制 Agent 调用 `skill_view`，也不新增工具、状态对象、tool gate 或 finalizer。

因此 `jobs.json.skills` 在 Cron user message 中的含义调整为“可参考使用的 Skill 名称”。
Agent 需要时可调用既有 `skill_view`；不调用不会被系统拦截或要求重试。这是刻意的语义
变化，用较小的接缝换取更短、职责更清晰的首条 user message。

## 当前问题

当前 `_build_job_prompt()` 产生的 user message 有五层：

| 层 | 当前内容 | 当前来源 |
| --- | --- | --- |
| 1 | `[IMPORTANT: The user has invoked ...]` | scheduler Skill wrapper |
| 2 | 完整 SKILL.md | scheduler 同步调用 `skill_view()` |
| 3 | `The user has provided ...` | scheduler Skill/task bridge |
| 4 | Cron delivery 与 `[SILENT]` 规则 | scheduler Cron hint |
| 5 | 任务 prompt、script/context 动态数据 | `jobs.json` 与运行时数据 |

真实任务 `39c23c0be177` 的一条 user message 约 13,572 字符，其中
`web-research-fallbacks` 的完整正文约 11,547 字符，约占 85%。这部分是静态工作流手册，
不是本轮用户任务或运行数据。

## 目标与边界

### 目标

1. 默认 Cron 首条 user message 不包含完整 Skill、Skill wrapper、Skill/task bridge 或
   Cron fixed hint。
2. 第 5 层的 script/context 注入顺序、wake gate、截断和错误语义保持不变。
3. 有配置 Skill 时，在第 5 层末尾追加紧凑的单行中文名称提示。
4. 自动交付、`[SILENT]`、workspace context、Artifact settlement、execution identity 与
   `no_agent=True` 的现有语义保持不变。
5. 普通 chat、voice、gateway、通用 tool dispatch、`skills_tool` 和 conversation loop 不变。

### 非目标

- 不保证模型读取每个声明 Skill，也不把 `jobs.json.skills` 当作强制工作流。
- 不新增 `CronSkillLoadPlan`、`cron_runtime_context`、Skill digest、tool gate 或 finalizer。
- 不修改 Skill 内容、SkillHub、`jobs.json` 字段结构、workspace 分配或 toolset 配置。
- 不保留 scheduler 的 bundle 正文展开；bundle 名称与普通 Skill 名称一样只作为参考文本展示。
- 不保证整轮 token 或 `state.db` 总字节数下降；Agent 主动调用 `skill_view` 时，完整正文仍会
  作为 tool result 进入后续上下文并可能持久化。
- 不修改 `no_agent=True` 的 script-only 路径。

## 改造后的提示词

### 首条 user message

假设 job 配置为：

```json
{
  "id": "daily12345678",
  "name": "每日比赛报告",
  "skills": ["score-report"],
  "prompt": "根据最新数据生成日报，写到 {{cron.workspace}}/日报.md"
}
```

本轮 workspace 为 `/workspace/sessions/cron/default/cron_daily_0900` 时，首条 user
message 是：

```text
根据最新数据生成日报，写到
/workspace/sessions/cron/default/cron_daily_0900/日报.md

本任务可参考使用以下技能：score-report
```

技能提示不使用 Markdown 标题、列表或调用指令，但保留与任务正文之间的空行。多个名称保持
job 的声明顺序并以中文逗号连接：

```text
本任务可参考使用以下技能：score-report，web-research-fallbacks
```

`skills` 为字符串、列表或 legacy `skill` 时沿用现有归一化方式。空值不产生提示；重复值
保持原声明，不在本次改动中新增去重或配置校验语义。

### 完整端到端示例

下面 job 同时配置 Skill、运行前脚本和上游输出：

```json
{
  "id": "daily12345678",
  "skills": ["score-report"],
  "prompt": "根据最新数据生成日报，写到 {{cron.workspace}}/日报.md",
  "script": "scripts/fetch_scores.py",
  "context_from": ["yesterday-snooker"]
}
```

本轮 execution 为：

```text
session_id = cron_daily12345678_20260907_090000_abcd1234
workspace  = /workspace/sessions/cron/default/cron_daily12345678_20260907_090000_abcd1234
```

脚本 stdout 为：

```text
今日完成 3 场比赛数据采集。
{"wakeAgent": true}
```

上游任务的最新输出为：

```text
昨日重点：张三晋级八强；今日首场将在 14:00 开始。
```

改造后的首条 user message 为：

````text
## Output from job 'yesterday-snooker'
The following is the most recent output from a preceding cron job. Use it as context for your analysis.

```
昨日重点：张三晋级八强；今日首场将在 14:00 开始。
```

## Script Output
The following data was collected by a pre-run script. Use it as context for your analysis.

```
今日完成 3 场比赛数据采集。
```

根据最新数据生成日报，写到
/workspace/sessions/cron/default/cron_daily12345678_20260907_090000_abcd1234/日报.md

本任务可参考使用以下技能：score-report
````

其中 script/context 的标题、说明、代码块和顺序与当前 `_build_job_prompt()` 一致；本次仅移除
完整 Skill 及旧第 1--4 层。`{"wakeAgent": true}` 是控制行，只用于 scheduler 是否唤醒
Agent 的判断，不进入 user message。

若 Agent 判断 `score-report` 有帮助，可使用既有工具：

```text
skill_view(name="score-report")
```

完整正文以该工具的 result 进入模型后续上下文。若 Agent 不调用，该 Cron 仍可继续正常执行和
交付最终回复。

### ephemeral system context

原第 4 层不删除，而是与已有 workspace instruction 合并为本轮 API 调用有效的 ephemeral
system context：

```text
## Cron execution
Default workspace: /workspace/sessions/cron/default/cron_daily_0900
Resolve relative output paths against this workspace.
Honor any explicit absolute or stable project path in the task unchanged.

## Cron delivery
Your final response is delivered automatically. Do not send it yourself.
If there is genuinely nothing new to report, reply with exactly [SILENT].
```

它不进入 user message 或展示 transcript。对于 legacy Cron，workspace 段为空，但 Cron delivery
段仍存在；对于 `no_agent=True`，两段均不构造，因为该路径本来不会创建 AIAgent。

### script 与 `context_from`

它们继续属于第 5 层。scheduler 先运行 `script` 并检查 wake gate，再读取 `context_from`
指定上游 job 的最新 Markdown 输出，将成功取得的动态数据按现有规则置于任务 prompt 前面。
最后才追加 Skill 名称提示。

| 条件 | 行为 |
| --- | --- |
| script 最后一条有效输出为 `{"wakeAgent": false}` | 静默结束；不创建 Agent 或 prompt。 |
| script 成功但无 stdout | 保持现有静默语义。 |
| script 失败 | 错误摘要按现有 `## Script Error` 形式进入第 5 层，Agent 生成失败报告。 |
| 上游 job 无目录、无 Markdown、内容为空或读取失败 | 静默省略对应段落。 |
| 无 script 且无可用上游输出 | user message 只有任务正文和可选 Skill 名称提示。 |

job ID 校验、最新文件选择和 8K 截断均保持现状。此次不改变动态数据来源、顺序或边界。

## 最小代码改动

所有代码改动都限制在：

`/Users/wzq/Downloads/NLP-PyProject/hermes-agent/cron/scheduler.py`

### Prompt 组装

1. 保留 `_prepare_cron_user_prompt()` 的 runtime token 展开职责。
2. 将现有 `_cron_workspace_instruction()` 扩展或改名为
   `_cron_execution_system_instruction(execution_workspace)`：保留 V1 workspace 段，并无条件
   添加 agent-backed Cron 的 delivery / `[SILENT]` 段。
3. 在 `_build_job_prompt()` 中保留 script/context 组装；删除 `skill_view`、`bump_use`、bundle
   展开、Skill wrapper 和 Skill/task bridge。
4. 使用一个局部 helper 从 `skills` 或 legacy `skill` 得到非空名称，并在 prompt 非空时追加：

```python
prompt += f"\n\n本任务可参考使用以下技能：{'，'.join(skill_names)}"
```

5. `_scan_assembled_cron_prompt()` 继续扫描最终 user message。由于完整 Skill 不再由 scheduler
   拼入，调用时不再以 `has_skills=True` 放宽扫描；script/context 仍按既有
   `has_injected_data=True` 路径处理。
6. `run_job()` 在既有 `AIAgent(...)` 调用处传入 ephemeral system context；不修改
   `agent_init.py`、tool dispatch、`tools/skills_tool.py`、finalizer 或 conversation loop。

新的数据流：

```text
pre-run script / wake gate
  -> _prepare_cron_user_prompt()
  -> _build_job_prompt()
       = script/context 第 5 层 + task + compact skill-name hint
  -> AIAgent(ephemeral_system_prompt=workspace + Cron delivery)
  -> run_conversation(user_message)
  -> normal tool loop; Agent may call existing skill_view
```

不新增 feature flag、持久化字段或 inline escape hatch。回滚只需还原本次 scheduler 改动，
不涉及 `jobs.json`、session、workspace 或 Manifest 数据迁移。

## 生命周期与失败语义

| 场景 | 行为 |
| --- | --- |
| 无 Skill 的 agent-backed Cron | user message 仅保留现有第 5 层。 |
| 有 Skill 且工具可用 | 名称提示出现；Agent 可自行调用现有 `skill_view`。 |
| 有 Skill 但 `skills` toolset 被禁用 | 名称提示仍出现；不新增失败或自动启用工具。 |
| 名称不存在 | 名称照原配置展示；不在 scheduler 预读、扫描或阻断。 |
| bundle 名称 | 按配置名称展示，不由 scheduler 展开 bundle 正文。 |
| 模型不调用 `skill_view` | 正常继续工具循环和最终交付。 |
| 模型调用 `skill_view` | 复用现有 handler、扫描、tool result 持久化和 Skill 使用计数语义。 |
| `wakeAgent=false` / script 空输出 | 保持现有不构造 AIAgent 的静默语义。 |
| `no_agent=true` | 完全不走 prompt 或 ephemeral system context 链路。 |
| provider fallback | 沿用当前 continuation、execution ID 和 workspace 语义。 |

## 测试计划

在 `hermes-agent/tests/cron/test_cron_execution_prompt.py` 与
`hermes-agent/tests/cron/test_scheduler.py` 添加或更新定向测试：

1. 有 Skill 的 user message 包含任务、现有 script/context 数据与单行中文名称提示。
2. user message 不含 Skill wrapper、Skill 正文 sentinel、Skill/task bridge 或 Cron delivery hint。
3. `skills` 字符串、列表、legacy `skill`、空值和保留声明顺序的行为正确。
4. prompt 组装不会调用 scheduler 内的 `skill_view` 或 `bump_use`。
5. V1 Cron 的 workspace 与 Cron delivery 都在 ephemeral system prompt，不在 user row。
6. legacy agent-backed Cron 只有 delivery system context；`no_agent=True` 不创建 AIAgent。
7. script stdout/error、`context_from`、`wakeAgent=false`、空 stdout 和 8K 截断保持现有行为。
8. 已有 workspace 权威值测试补充断言：`SessionDB.ensure_session(cwd=...)`、`TERMINAL_CWD` 和
   V1 ephemeral workspace 使用同一 execution tuple 值。

验证命令：

```bash
scripts/run_tests.sh tests/cron/test_cron_execution_prompt.py tests/cron/test_scheduler.py -q
```

## 验收标准

- 首条 user message 保留第 5 层任务和动态数据，末尾有紧凑的 Skill 名称提示。
- 完整 Skill、旧 wrapper、bridge 和 Cron delivery hint 不进入首条 user message。
- Cron delivery / `[SILENT]` 与 V1 workspace 说明通过 ephemeral system context 有效。
- Agent 未调用 `skill_view` 时不被 gate 或 finalizer 阻断；调用时复用已有工具语义。
- 不新增 Cron 专用工具、Agent 状态、通用 tool dispatch 或 finalizer 改动。
- workspace identity、Artifact settlement、`no_agent=True`、wake gate 与 provider fallback 保持不变。
