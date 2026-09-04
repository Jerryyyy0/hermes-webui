# Cron Lazy Skill 提示注入方案

- **Status:** Proposed
- **Author:** Hermes WebUI maintainers
- **Created:** 2026-09-03
- **Related:** [Cron Execution Identity 与 Artifact 结算修复](cron-session-manifest-artifact-settlement.md)

## 决策摘要

Cron 的首条 user message 不再内联完整 skill 正文。`_build_job_prompt()` 改为：

1. 不向 user message 注入旧的第 1--4 层；
2. 保留第 5 层，即用户保存的任务 prompt（含本轮 runtime token 展开）；
3. 紧跟任务 prompt 追加中文的已配置 skill 名称及加载要求；
4. Agent 在开始执行前通过 `skill_view(name)` 取得完整正文，正文以 tool result 而非
   user message 到达模型；
5. 原来的 Cron delivery / `[SILENT]` 规则移入 ephemeral system context，不再属于
   user message。

这不是纯展示压缩，而是把 job 的 skill 交付方式从 inline 改为 lazy load。模型最终仍会
看到并遵循 skill 正文；减少的是首个 user message 和首个模型请求的输入大小，不保证减少
整轮 token、数据库总大小或后续 replay 的上下文长度。

## 当前问题

当前 `_build_job_prompt()` 产生的 user message 有五层：

| 层 | 当前内容 | 当前来源 |
| --- | --- | --- |
| 1 | `[IMPORTANT: The user has invoked ...]` | scheduler skill wrapper |
| 2 | 完整 SKILL.md | scheduler 同步调用 `skill_view()` |
| 3 | `The user has provided ...` | scheduler skill/task bridge |
| 4 | Cron delivery 与 `[SILENT]` 规则 | scheduler Cron hint |
| 5 | 用户的任务 prompt，以及可选 script/context 数据 | `jobs.json` 与运行时数据 |

真实任务 `39c23c0be177` 的一条 user message 约 13,572 字符，其中
`web-research-fallbacks` 的完整正文约 11,547 字符（约 85%）。该正文是静态工作流
手册，不是用户本次实际输入。

## 目标与非目标

### 目标

1. 默认 Cron 首条 user message 不包含完整 skill、skill wrapper、skill/task bridge 或
   Cron fixed hint。
2. 用户任务 prompt 始终是 user message 的第一部分；skill 名称以中文 block 追加在其后。
3. 声明了 `skills` 的 Cron 在首次有意义的执行动作前，必须成功加载全部声明 skill。
4. `skills` 工具集在这种 Cron 中必定可用；不可用、缺失或安全扫描失败时，在 Agent
   做副作用前明确失败。
5. 保持 Cron 自动交付、`[SILENT]`、workspace context、Artifact settlement、execution
   identity 与 `no_agent=True` 的已有语义。
6. 普通 chat、voice、gateway 与非 Cron Agent 不改变 skill 注入或工具语义。

### 非目标

- 不让 Agent 完全不知道 skill 正文；它仍需在 `skill_view` tool result 中读取正文。
- 不承诺总 token 或 `state.db` 总字节数下降。完整正文在 tool result 后仍进入模型历史，
  并可能持久化为 tool row。
- 不把 job `skills` 的语义降级为“模型可忽略的推荐项”。历史上它表示本轮应遵循的工作流。
- 不移除 Cron 的 delivery / `[SILENT]` 行为，只把它从 user 层迁移到 ephemeral system 层。
- 不修改 skill 内容、SkillHub 安装流程、`jobs.json` 的 skill 字段格式或 workspace 分配。
- 不修改 `no_agent=True` 的 script-only 运行路径。

## 新的提示词与工具链

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
message 必须是：

```text
根据最新数据生成日报，写到
/workspace/sessions/cron/default/cron_daily_0900/日报.md

## 可用技能

本任务可参考使用以下技能。需要使用某项技能时，请调用
`skill_view(name=...)` 查看其完整规则：

- `score-report`
```

它不再包含第 1、2、3、4 层，也不直接包含 script stdout 或 `context_from` 正文。

若没有声明 skill，则不追加 `## 可用技能` block。`skills` 为字符串、列表或 legacy
`skill` 时使用同一个归一化列表；名称按 job 配置的声明顺序去重。未知/空名称不能写入
prompt，而是在 Agent 启动前返回明确的 job 配置错误。

#### `script` 与 `context_from` 同时存在的例子

若同一 job 还配置了运行前脚本和上游任务输入：

```json
{
  "id": "daily12345678",
  "skills": ["score-report"],
  "prompt": "根据最新数据生成日报，写到 {{cron.workspace}}/日报.md",
  "script": "scripts/fetch_scores.py",
  "context_from": ["yesterday-snooker"]
}
```

本轮执行顺序为：

```text
1. scheduler 执行 scripts/fetch_scores.py，并按现有规则通过 wake gate。
2. scheduler 收集 stdout/error，以及 yesterday-snooker 的最新输出；
   它们暂存为本轮 execution 的 runtime context，不拼接到首条 user message。
3. Agent 收到的首条 user message 仍只有“任务正文 + 可用技能名称”，形状与上例相同。
4. Agent 读取 score-report 后，调用只读的 cron_runtime_context。
5. 该 tool result 返回已收集的脚本和上游结果，Agent 再据此完成任务。
```

第 4 步的 tool result 例如：

```text
## Pre-run script output

赛事接口返回：今日已结束 3 场；待开赛 2 场；数据更新时间 09:02。

## Output from job 'yesterday-snooker'

昨日重点：某选手晋级八强；今日首场将在 14:00 开始。
```

脚本成功但没有 stdout 时，保持现有静默语义：不启动 Agent，也不会产生该 tool result。
未找到 `context_from` 的上游输出时，只省略对应段落，不把配置字段或空占位文本写入
user message。脚本失败时，沿用当前 agent-backed Cron 语义：将错误摘要放入同一 runtime
context，仍唤醒 Agent 生成面向用户的失败报告。

### ephemeral system context

原第 4 层不应被删除，而是和已有 workspace instruction 合并为只在本轮 API 调用有效的
ephemeral system context：

```text
## Cron execution
Default workspace: /workspace/sessions/cron/default/cron_daily_0900
Resolve relative output paths against this workspace.
Honor any explicit absolute or stable project path in the task unchanged.

## Cron delivery
Your final response is delivered automatically. Do not send it yourself.
If there is genuinely nothing new to report, reply with exactly [SILENT].
```

它不进入 user message 或展示 transcript。`[SILENT]` 的解析和 delivery adapter 保持现有
行为；迁移只能改变提示词层级，不能改变 marker 文本或 delivery 逻辑。

### script 与 `context_from`

为满足“第 1--4 层都不注入、仅在第 5 层后追加 skill 名称”的规则，本 RFC 同时将
script stdout/error 与 `context_from` 正文从首条 user message 移至各自的受控 tool result：

```text
pre-run script succeeds
  -> scheduler collects its result into this execution's runtime context
  -> Agent calls cron_runtime_context after loading a skill
  -> tool dispatcher returns the collected script result

context_from latest output is available
  -> scheduler collects it into this execution's runtime context
  -> Agent calls cron_runtime_context
  -> tool dispatcher returns the collected upstream result
```

这里的 `cron_runtime_context` 不是 scheduler 主动插入的一条消息，也不是通用的文件或命令
工具。scheduler 在 Agent 启动前只做两件事：运行已配置的 pre-run script，并根据
`context_from` 读取已允许的上游 job 最新输出。它把结果绑定到当前 execution session 的
只读 runtime context；模型看不到这些暂存数据，直到自己发起 `cron_runtime_context()` tool
call。tool dispatcher 只返回当前 execution 已收集的内容，不接受模型提供的路径、命令或
job ID。

以如下 job 为例：

```json
{
  "prompt": "生成今日战报，保存到日报.md",
  "skills": ["score-report"],
  "script": "scripts/fetch_scores.py",
  "context_from": ["yesterday-job"]
}
```

假设 pre-run script 收集到“今日完成 3 场比赛数据采集”，而 `yesterday-job` 的最近输出为
“张三晋级八强”。新运行路径为：

```text
1. scheduler 执行 scripts/fetch_scores.py，并先检查其 wake gate。
2. gate 允许唤醒后，scheduler 将脚本输出和上游输出绑定到本轮 execution。
3. Agent 首条 user message 仍仅含任务正文和 score-report 名称。
4. Agent 调用 skill_view(name="score-report")，取得完整 skill 正文。
5. Agent 调用 cron_runtime_context()，得到以下 tool result：

   ## Pre-run script output
   今日完成 3 场比赛数据采集。

   ## Output from job 'yesterday-job'
   张三晋级八强。
6. Agent 使用该 tool result 和 skill 规则完成报告。
```

分支语义保持现有契约：

| 条件 | 新方案行为 |
| --- | --- |
| script 最后一条有效输出为 `{"wakeAgent": false}` | 本轮静默结束；不启动 Agent、不加载 skill，也不创建可读取的 runtime context。 |
| script 成功但没有业务 stdout | 保持现有静默语义，不启动 Agent。 |
| script 失败 | 仍唤醒 agent-backed Cron；错误摘要进入 runtime context，供 Agent 生成失败报告。 |
| 上游 job 无目录、无 Markdown、内容为空或读取失败 | 静默省略该 job 对应段落；不写入空占位或错误文本。 |
| 无 script 且无可用上游输出 | 不注册本轮 `cron_runtime_context`；Agent 不需要调用它。 |

`context_from` 的读取上限、有效 job ID 校验和“取最近 Markdown 输出”的规则沿用现有
`_build_job_prompt()` 行为；此次改动只改变内容交付位置，不扩展它可读取的范围。

这个 internal-only `cron_runtime_context` tool 不是通用文件读取能力：它只返回 scheduler
已经按当前规则取得的 script stdout/error 与允许的上游 job 输出，并携带来源类型和 job
ID；不接受模型提供的路径、命令或 source ID。内容保留现有大小限制。没有数据时不创建
tool result。这样 user message 的第 5 层始终只表示用户任务，而不是动态采集数据。

## 必需的执行约束

仅在 user message 中写“可用技能”不够。语言模型可能跳过 `skill_view`、先调用 web 或
terminal，甚至直接给出最终答复。为保持现有 `job.skills` 的强语义，Cron Agent 必须持有
每轮独立的 `CronSkillLoadPlan`：

```text
execution session ID
  -> required skill names
  -> preflight-resolved canonical skill paths
  -> content digest + scanner result
  -> loaded skill names for this turn
```

约束规则：

1. scheduler 在构建 Agent 前解析每个已配置 skill、确认存在，并按 Cron 专用规则扫描
   正文；此步骤**不把正文拼入 prompt**。
2. 该 job 必须拥有 `skill_view`；`enabled_toolsets` 缩窄配置时，scheduler 必须为本轮
   加入最小 `skills` toolset，或在启动前以 `required_skill_tool_unavailable` 失败。
3. Agent 只要尚未加载 `CronSkillLoadPlan` 中全部 skill，工具调度器只允许对应的
   `skill_view(name)`；web、terminal、file、MCP 及其它产生外部副作用的工具必须返回
   `required_skill_not_loaded`。
4. Agent 若在未完成加载时尝试直接结束，turn finalizer 必须把该结果视为未满足的
   required-skill precondition，要求它调用缺失的 `skill_view`，而不是交付报告。
5. `skill_view` 对 Cron required skill 必须读取本轮 preflight 的 canonical path/digest；
   若文件在 preflight 后变更、被替换或重定向到不同内容，返回
   `skill_content_changed_before_load`，不交付正文。
6. `skill_view` 返回正文前复用 Cron fail-closed scanner。普通 interactive `skill_view`
   的“告警后继续返回”语义不能用于这条自动执行路径。
7. 全部 skill 成功加载后，才允许 `cron_runtime_context` tool 返回脚本/上游数据；之后
   才恢复正常工具集与任务执行。

这组 guard 必须在 tool dispatch/finalization 层实现，而不能只依赖中文 prompt。prompt
说明使用“可用技能”，但 `jobs.json.skills` 的实际执行语义仍是 required；未来若要支持
真正可选的 skill，应新增显式 `optional_skills` 字段，不能改变已有字段的含义。

## 代码改动

### Agent：prompt 组装

文件：`/Users/wzq/Downloads/NLP-PyProject/hermes-agent/cron/scheduler.py`

将当前 `_build_job_prompt()` 拆为职责明确的私有 helper：

```python
def _resolve_cron_skill_load_plan(job: dict) -> CronSkillLoadPlan: ...
def _prepare_cron_user_prompt(...): ...  # 保持 token 展开职责
def _build_lazy_skill_notice(plan: CronSkillLoadPlan) -> str: ...
def _build_cron_user_message(prepared_user_prompt: str, plan: CronSkillLoadPlan) -> str: ...
def _cron_execution_system_instruction(workspace: str | None) -> str: ...
```

新的 `run_job()` 数据流：

```text
pre-run wake gate
  -> _prepare_cron_user_prompt()
  -> _resolve_cron_skill_load_plan()        # resolve + digest + scan，未注入正文
  -> _build_cron_user_message()
       = prepared user task + Chinese skill notice
  -> AIAgent(..., cron_skill_load_plan=plan,
             ephemeral_system_prompt=_cron_execution_system_instruction(...))
  -> run_conversation(user_message)
  -> enforced skill_view(s)
  -> cron_runtime_context tool result（若存在）
  -> normal task tool loop
```

`_build_job_prompt()` 可作为兼容 facade 保留，但其 Cron 默认路径必须返回新 user message；
旧的 inline builder 移为私有 `_build_inline_skill_prompt()`，只服务隔离兼容测试或未来
明确 opt-in 的 legacy mode。不能在同一默认路径中先拼接全文、再从字符串删除；这样会让
扫描、日志和异常路径再次泄漏正文。

### Agent：工具与状态接缝

| 文件/模块 | 具体改动 |
| --- | --- |
| `agent/agent_init.py` | 保存 immutable `cron_skill_load_plan` 与 per-turn loaded-name set；初始化时深拷贝，避免 provider fallback/并发 Agent 串用状态。 |
| `agent/tool_executor.py` 或现有工具 dispatch 统一 gate | 调用 tool 前检查 required skills 是否已加载；只放行所需 `skill_view`，其余工具返回结构化拒绝。 |
| `tools/skills_tool.py` | 为 Cron required-skill 调用增加 plan-aware handler：验证 canonical path、digest、Cron scanner 后返回正文，并标记为已加载。不得修改普通 interactive skill_view 的兼容行为。 |
| `agent/turn_finalizer.py` | 在最终答复前验证所有 required skills 已加载；未加载时把缺失名称作为下一轮强制 tool action，而不交付最终文本。 |
| `cron/scheduler.py` / Cron 专属工具模块 | 注册只读、参数不可控的 `cron_runtime_context`；将既有 script/context 采集结果延后以 tool result 形式提供。 |
| `agent/conversation_loop.py` | 保持 tool result 顺序和持久化；不把 tool result 回填到首条 user message。 |

`CronSkillLoadPlan` 必须按 execution session ID 绑定，provider fallback 共享同一次 execution
的同一 plan；下一次 automatic trigger 创建新 plan，重新读取并扫描当前 skill 内容。

### WebUI

本方案不改 WebUI HTTP、SSE、Manifest schema 或 Cron Hub API。WebUI 会自然展示更短的
首条 user message；完整 skill 和 runtime data 若由 Agent 持久化，则展示为对应 tool call
及 tool result，而不是伪装成用户输入。

## 生命周期与失败语义

| 场景 | 行为 |
| --- | --- |
| 无 skill 的 agent Cron | user message 仅为用户任务；不创建 plan，不增加 guard。 |
| 有 skill，全部存在且安全 | user message 为任务 + 中文名称；按顺序强制 `skill_view` 后开始工作。 |
| skill 缺失 | AIAgent 启动前失败，持久化明确错误摘要；不调用 provider。 |
| preflight scanner 拒绝 | AIAgent 启动前失败；不把被拒绝正文写入 user message、tool row 或日志。 |
| `skill_view` digest 变化 / TOCTOU | 拒绝正文和后续动作，结束为 `cron_error`。 |
| 模型跳过 skill、直接工具调用 | tool gate 返回 `required_skill_not_loaded`；模型只能补齐 `skill_view`。 |
| 模型跳过 skill、直接最终答复 | finalizer 拒绝交付，要求加载缺失 skill。 |
| `wakeAgent=false` | 与现状相同：不建 prompt、不读 skill、不创建 plan、不启动 Agent。 |
| script 成功但无 stdout | 保持现有静默语义，不创建 runtime context tool。 |
| `no_agent=true` | 完全不经过 lazy skill / runtime-context 链路。 |
| provider fallback | 共用 execution ID、workspace、plan 和已加载 skill 状态；不重新创建 workspace。 |

## 测试计划

### Prompt 形状

在 `hermes-agent/tests/cron/test_cron_execution_prompt.py` 新增断言：

1. 有 skill 的默认 user message 只含 prepared task 和中文 `## 可用技能` block；
2. 不含旧 wrapper、完整 skill sentinel、skill/task bridge、Cron hint、script stdout 或
   `context_from` 正文；
3. skill 名称顺序、去重、legacy `skill` 兼容、空 skill 和 runtime token 展开正确；
4. Cron delivery/workspace instruction 存在于 ephemeral system prompt，且不在 user row；
5. 无 skill job 不注入空 notice。

### 执行与安全

在 `tests/cron/test_scheduler.py`、`tests/tools/test_skills_tool.py` 与 tool-dispatch 测试中
覆盖：

| 场景 | 必须断言 |
| --- | --- |
| required skill 正常加载 | 首个 user message 无正文；`skill_view` tool result 有正文；随后工具可用。 |
| 未加载时 web/terminal/file/MCP | 被统一 gate 拒绝，错误码为 `required_skill_not_loaded`。 |
| 未加载时最终答复 | 不交付，finalizer 继续要求 `skill_view`。 |
| missing skill | provider 零调用、无副作用、明确错误。 |
| scanner-rejected skill | provider 零调用、正文不写入 DB/日志。 |
| preflight 后内容变更 | digest 检查拒绝，不向模型返回更改后的正文。 |
| `enabled_toolsets` 排除 skills | scheduler 强制加入最小 skill_view 或启动前明确失败；二者选择固定且有测试。 |
| script/context | 首条 user message 不含动态正文；required skill 加载后由 `cron_runtime_context` tool result 提供。 |
| `[SILENT]` | system context 提供规则；exact marker 继续抑制 delivery。 |
| fallback | 同一 execution 不重复 plan、skill tool result 或 user message。 |
| no-agent / wake false | 不加载 skill、不增加额外 provider/tool call。 |

### 回归验证

同一 fixture 在旧 inline 路径与新 lazy 路径分别执行，验证：

- 最终文件、最终回答和 Artifact decision 相同；
- 新路径的首个 provider 请求不含 skill sentinel；`skill_view` 后的下一请求包含它；
- 新路径多出恰好一次每 required skill 的 `skill_view` 轮次；
- Cron delivery 只发送一次，`[SILENT]` 不发送；
- SessionDB 的首条 user `content` 不含 skill 正文，skill 正文只出现在允许的 tool row。

## 发布与回滚

这是默认 prompt/工具语义变更，必须分阶段发布：

1. **观察模式**：scheduler 创建并扫描 plan、记录“本轮会要求加载哪些 skill”，但仍使用
   inline prompt；验证现有 jobs 的工具集和 skill 可用性。
2. **Opt-in lazy mode**：通过受控 profile/job feature flag 启用新路径，验证真实 Cron 的
   tool gate、delivery、fallback 和 Artifact。
3. **默认 lazy mode**：观察模式无缺失/扫描/工具集异常后，才将新规则设为默认。
4. **保留 inline escape hatch**：紧急回滚只切回 inline builder，不改 jobs.json、session、
   workspace 或 Manifest rows。

观测只记录 job ID、session ID、plan skill 数、首次请求字符数、skill-load 成功/失败、
gate 拒绝次数、最终状态和错误码。不得记录 skill 正文、任务 prompt、script/context 内容
或 workspace 绝对路径。

## 验收标准

- 默认 Cron 首条 user message 仅有用户任务和其后的中文 skill 名称 block。
- 完整 skill、旧 wrapper、bridge、Cron hint、script/context 正文均不进入首条 user message。
- Cron delivery / `[SILENT]` 在 ephemeral system context 中仍有效。
- 每个声明 skill 在任何副作用工具或最终交付前都已成功通过 plan-aware `skill_view` 加载。
- 缺失、内容变化或 scanner 拒绝均在副作用前失败，且不泄漏正文。
- tool result 形式的 skill/runtime context 能被模型正常消费，Artifact settlement 与 workspace
  identity 不变。
- 普通 chat、legacy inline escape hatch、`no_agent=true` 和 `wakeAgent=false` 行为不变。
