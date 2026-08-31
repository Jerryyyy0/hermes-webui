# Cron Execution Identity 与 Artifact 结算修复

- **Status:** Proposed
- **Author:** Hermes WebUI maintainers
- **Created:** 2026-08-29

## 问题

两个已完成的 Cron 会话均成功产生了可交付文件，却没有出现在 Session Inspector 或聊天轮次的 Artifacts 中。

复现显示，这不是某一种文件类型、某个工具或 Manifest 路径过滤造成的偶发问题：

- 一次运行通过 `terminal` 成功生成 HTML，最终 assistant 明确交付了该绝对路径；
- 另一次运行通过成功完成的 `write_file` 写入 Markdown，工具调用与结果按相同 call ID 正确配对；
- 将第二条真实 transcript 放入隔离的现有 `_persist_turn_artifact_paths()` 结算器，会得到一个非空、可预览的 `write_file` Artifact decision；
- 已保存的 Cron 会话缺少对应 `session_manifest.db` decision。Manifest GET 按既有契约只读，因此打开会话不会恢复该结果。

第二次运行还把创建任务时的普通 session 目录写进了持久化 prompt。后续每次 trigger 虽然获得新的 `cron_*` session 和 workspace，却仍按该显式路径写回创建会话目录。

隔离回放证明，这条路径不是 Artifact 缺失的原因。产品允许用户在 prompt 中指定具体目录，因此这不是需要拒绝的坏配置；真正的 prompt 缺口是 assembled prompt 没有声明本轮默认 session/workspace，无法为相对路径和可选 runtime token 提供明确上下文。

## 根因

### 直接物化路径遗漏结算

`integration/crons/session_bridge.py:materialize_cron_session()` 会把 Agent `state.db` 中的 `source=cron` transcript 物化为 WebUI sidecar，但它自身不执行 Manifest turn settlement。

WebUI 内的 scheduler wrapper 会走 `materialize_after_cron_run()`，后者会加载 sidecar 并调用 Cron prepare/settlement。因此只有运行恰好经过该进程内 wrapper 时才得到 Artifact decision。

这解释了手动/自动差异：WebUI 手动执行在装有 hook 的进程中运行，结束时立即结算；自动 trigger 由独立 Agent scheduler 进程执行，该进程没有 WebUI 的内存 monkey patch。

自动运行结束后，WebUI 只能从 `state.db` 或 history/output 再物化；这些 direct materialization 路径当前没有结算，所以文件能入 workspace、会话能显示，Artifact decision 却缺失。

但以下入口直接调用 `materialize_cron_session()`，然后只读取或 reconcile transcript：

- `GET /api/session` 在缺少 WebUI sidecar 时精确恢复指定 Cron session；
- Cron recent/history/output 的恢复和 backfill；
- 跨 Profile 的 completion materialization；
- 任何未来为既有 Agent Cron run 建立 sidecar 的直接调用方。

这些入口可成功展示会话，却绕过 Artifact settlement。

结果是，同一个 Cron execution 是否出现 Artifact 取决于它被哪个入口首先物化。这违背了“成果由完成的会话活动决定，而不由查看路径决定”的不变量。

### Prompt 缺少本轮默认 workspace 上下文

受影响 job 在普通会话 `bbbad785a663` 中创建。`cronjob(action="create")` 将该会话的绝对目录原样写进 `jobs.json`，后续没有更新。

Agent V1 已为每个 trigger 生成新的 `_cron_session_id` 和 `_cron_execution_workspace`。同一 root 也写入 `state.db.sessions.cwd`、`TERMINAL_CWD` 和 WebUI sidecar。

缺口位于 prompt：`_build_job_prompt()` 只读取静态 `job["prompt"]`，没有加入本轮 execution identity。显式绝对路径按用户意图覆盖运行时 cwd 是允许行为；但未指定绝对路径的任务也看不到本轮默认 workspace，且无法使用本轮动态值。

## 目标

1. 每个 trigger 只产生一个 immutable execution identity：`(profile, session_id) -> workspace_root`。
2. job prompt 保留用户原文并允许具体路径；运行时追加本轮默认 session/workspace，但 execution identity 不能从 prompt 反向推导。
3. 已完成 transcript 一旦物化为 sidecar，每个真实 turn 都有 non-empty 或 empty Manifest decision。
4. follow-up 复用所属 execution 的原 session ID/workspace；下一次 automatic trigger 使用新的 session ID/workspace。
5. manual、automatic、history、exact-run、batch recovery 和 follow-up 得到一致且幂等的 identity 与 Artifact 结果。
6. `GET /api/session/manifest` 保持只读，不 backfill、不 repair、不更新 sidecar 或 Artifact store。
7. 历史 Artifact 缺口通过显式、可审计的维护操作修复；历史 job 路径只做可选审计，不强制迁移。
8. 保持现有 Artifact 证据、路径安全、profile/lineage/workspace-root 隔离及 preview 契约。
9. prompt 路径形式不新增配置失败；pause、disable、delete、scheduler loop 和其它 jobs 保持既有行为。

## 非目标

- 不把 Manifest 变成 workspace 文件列表，也不从 terminal stdout、搜索结果、目录扫描或中间 assistant 文本猜测成果。
- 不改变 Cron workspace 的分配算法、目录布局或 `state.db` schema。
- 不新增 `CronExecutionContext` 或重构 `cron/execution_workspace.py`；现有 V1 tuple 已足以作为本轮权威值。
- 不修改 file、terminal、code-exec 工具，也不引入通用文件 sandbox。
- 不让 Manifest GET 自动移动、复制、删除、哈希或修复源文件。
- 不静默重写用户 prompt 中语义不明确的绝对输入路径。
- 不批量改写 Cron job、skill 或 Memory；固定路径是否改成相对路径或 runtime token 由用户决定。
- 不承诺阻止任意 terminal 脚本写出 workspace。若需要 OS 级隔离，应建立独立 RFC。
- 不引入新的 Manifest HTTP/SSE schema、数据库表或前端长期状态。

## 现有状态与必须保留的不变量

| 状态/组件 | 当前权威来源 | 修复后必须保持 |
| --- | --- | --- |
| Execution identity | Agent 每轮创建的 `session_id` 与 allocated root | `(owner_profile, session_id)` 唯一映射到 immutable `workspace_root` |
| Stored Cron job | Agent `jobs.json` | 保存用户 prompt 原文，可含具体路径；不得把其中路径当作 execution identity 的权威来源 |
| Cron continuation | WebUI sidecar 的 `session_id/workspace/mode/state` | `ready` 的 managed/worktree session 只复用原 root，不重新 allocation 或切换 root |
| Cron transcript | Agent `state.db` 与 WebUI sidecar 的既有 reconcile 规则 | sidecar 先持久化，再产生 Artifact decision |
| Artifact decision | `{HERMES_WEBUI_STATE_DIR}/session_manifest.db` | 以 `lineage + profile + workspace_root + turn_key + record_kind + path` 隔离 |
| Artifact 证据 | 成功 completed 工具、`MEDIA:`、成功 skill mutation、末条 assistant 的严格路径 | 不新增弱证据来源 |
| Manifest GET | `build_session_manifest()` 的 store-first 投影 | 永远只读，不触发历史回填 |

`session_id` 是 execution identity，workspace 是该 identity 的 immutable 属性。消费者只能读取这对值，不能从目录名反推 session，也不能从历史 prompt 恢复 workspace。

特别地，`empty` decision 与“没有 decision”不同：前者是已结算且没有合法成果，后者是本 RFC 要消除的生命周期漏洞。repair 不能覆盖已有 non-empty decision。

四类 ID 不得混用：`job_id` 标识重复任务定义；`session_id` 标识一次 trigger；`workspace_root` 是该 execution 的目录属性；Manifest `lineage_key/turn_key` 标识 sidecar 中的逻辑轮次。

同一 job 的下一次 trigger 必须复用 `job_id`，但必须生成新的 `session_id` 和 `workspace_root`。历史 sidecar 恢复复用原 execution 的 `session_id/workspace_root`，不得重新分配。

## 方案

### 1. 在现有 V1 execution tuple 上绑定 prompt

Agent 当前的 `_prepare_v1_execution_workspace()` 已一次性返回 `(session_id, cwd, strategy)`，并把同一组值用于 `job["workdir"]`、`state.db`、AIAgent 和工具 cwd。

本次不修改 `cron/execution_workspace.py`，不新增 `CronExecutionContext`，也不改变 workspace 分配、worktree cleanup 或 legacy job 行为。

`run_job()` 在开头解包现有 tuple。V1 后续代码直接使用这两个局部变量，不能再次从 prompt、目录名或历史 session 推导值：

```text
execution_session_id
  -> AIAgent.session_id
  -> state.db.sessions.id
  -> WebUI sidecar.session_id

execution_workspace
  -> SessionDB.ensure_session(cwd=...)
  -> TERMINAL_CWD / script cwd
  -> state.db.sessions.cwd
  -> WebUI sidecar.workspace
```

现有 `_cron_session_id`、`_cron_execution_workspace` 和 `job["workdir"]` 继续作为本轮内存兼容字段，不能回写 `jobs.json`。

pinned-provider → global fallback 继续使用当前 continuation 机制。正常 trigger 必须从新建的 execution job copy mint 新的 session ID；只有同一 `run_job()` 调用内、明确带 `_cron_session_continuation=True` 的 provider fallback 才能复用 primary attempt 的 transient session ID/root。不允许因为复用同一个 job dict、重读 `jobs.json` 或看到 `_cron_session_id` 就跨 trigger 复用。

WebUI 恢复仍以精确 `state.db` row 为 durable adapter。Manifest root 继续由已验证 sidecar workspace 按现有 Artifact root 规则计算，不能取自 stored prompt。

#### 1.1 让静态 job prompt 绑定现有运行时值

`jobs.json` 保存用户的语义任务和原始路径文本；路径可以是相对路径，也可以是用户明确指定的绝对路径。需要显式引用本轮值时，可使用两个保留 token：

```text
{{cron.session_id}}
{{cron.workspace}}
```

`cron/scheduler.py` 新增纯函数 `_prepare_cron_user_prompt(prompt, execution_session_id, execution_workspace)`，只负责保留 stored prompt 并按可用 binding 展开 runtime token；不对用户指定的目录做拒绝校验。workspace 说明不进入 user prompt，而通过 Agent 已有的 `ephemeral_system_prompt` 注入本轮 API 请求。

`run_job()` 保留现有 wake-gate 顺序：先执行 pre-run script；脚本失败仍按既有失败语义返回，`wakeAgent=false` 仍静默结束本轮。对已创建 V1 execution，静默返回前必须写入正常结束边界（`cron_complete`，不展开 prompt、不写失败消息），避免留下无法 settlement 的 active `state.db` row。只有 gate 通过后，才调用 `_prepare_cron_user_prompt()`，再把结果交给 `_build_job_prompt(..., prepared_user_prompt=...)`。后者只负责注入 skill、script output 与 `context_from` 数据。

因此用户在 prompt 中指定的绝对路径不会被调度器改写或拦截。不会唤醒 Agent 的 tick 不会构造或展开 prompt，保持现有 wake-gate 兼容性；外部数据或 skill 示例中的同名 token 也不会被展开。

token 展开只影响本轮 assembled prompt，不修改 `jobs.json`。V1 binding 完整时替换两个 token；缺少 binding 时保留 token 原文，不阻止本轮运行。

含 runtime token 的 V1 job 在有 binding 时展开为本轮值。没有 V1 binding 的 legacy job 不展开 token，保留原文并按既有 legacy 语义运行；runtime token 只是便利写法，不是阻止任务运行的配置门禁。

V1 execution 通过 `ephemeral_system_prompt` 注入简短 runtime context，说明默认 workspace、相对路径解析规则，以及用户显式路径的优先级；该 context 不写入 SessionDB transcript，legacy job 不注入：

```text
## Cron execution
Default workspace: <execution_workspace>
Resolve relative output paths against this workspace.
Honor any explicit absolute or stable project path in the task unchanged.
```

文件交付优先使用相对文件名，并从 `write_file` 的 `resolved_path` 构造 `MEDIA:`。只有确需绝对路径文本时才使用 `{{cron.workspace}}`。

#### 1.2 允许用户显式指定目录，不把 prompt 当作安全边界

`jobs.json[].prompt` 是用户任务定义，允许保存相对路径、稳定项目路径以及用户明确指定的绝对路径，包括历史 session 目录。创建和更新时不调用 `validate_cron_prompt_execution_paths()`，也不新增 `CronPromptConfigurationError` 异常族；prompt 原文按现有持久化语义保存。

`cron/jobs.py` 与 `tools/cronjob_tools.py` 保持现状：prompt 仍是普通字符串，不增加目录格式限制。`no_agent=True` 仍由 script 作为任务本体，保持现有 script path 校验与执行语义。

`run_job()` 不再做 concrete path runtime backstop，也不会因为 prompt 中出现旧 session root 而 fail closed。V1 tuple 仍是 state.db、AIAgent、cwd 和 sidecar 的唯一运行时来源；prompt 中的路径是用户意图，不能反向改变该 tuple。

这意味着 workspace context 是提示词级的软约束：模型通常会按当前 workspace 生成相对输出，但用户明确写入的绝对路径可以覆盖它，terminal 或脚本也可以写到 workspace 之外。本 RFC 不声称提供文件系统 sandbox。

Artifact settlement 与 prompt 路径解耦：系统不扫描 prompt 或目录来猜测成果，只消费已有受控工具证据、`MEDIA:` 和最终 assistant 交付路径。用户指定的 root 外绝对路径允许执行；若有符合现有白名单的成功 mutation/terminal 或最终交付证据，并通过 `external_references.policy`，可按既有契约登记为当前 execution 的外部直接引用。仅在 prompt 中提到路径不构成 Artifact 证据。

`_finish_pre_agent_silent()` 仍只负责结束已创建的 V1 execution；`_persist_pre_agent_failure()` 继续处理真实的脚本、provider 或 Agent 启动失败。prompt 路径不再制造额外的配置失败分支，scheduler 仍按既有 finally 释放 claim、结束 session 并继续其它 job。

最小更新状态机：

| 操作 | prompt 含具体目录 | 是否允许 | 持久化/运行结果 |
| --- | --- | --- | --- |
| create / update agent-backed job | 是 | 是 | `jobs.json[].prompt` 原文保存 |
| pause / disable / resume / trigger | 是 | 是 | 沿用既有 job 与 execution 语义，不因路径形式失败 |
| delete | 是 | 是 | 删除流程不读取或改写 prompt |
| automatic runtime | 是 | 是 | 仍按 V1 tuple 建立 session/workspace；workspace context 仅通过 ephemeral system prompt 提示 |
| no-agent script job | prompt 任意 | 是 | prompt 不参与执行，沿用 script 校验与执行语义 |

用户指定的 root 外路径可以执行；是否进入当前 execution 的 Artifact 取决于既有成功工具/最终交付证据与 external-reference 安全 gate，而不是 prompt 路径形式。

#### 1.3 Agent：最小文件接缝与测试

| Agent 文件 | 具体改动 | 核心验证 |
| --- | --- | --- |
| `cron/scheduler.py` | 解包既有 V1 tuple；保留 pre-run script/wake-gate 顺序，`wakeAgent=false` 结束 V1 execution 后静默返回；gate 通过后保留 stored prompt 原文并按可用 binding 展开 token；workspace context 通过 `AIAgent(ephemeral_system_prompt=...)` 注入，不进入 user prompt。 | state.db/cwd、AIAgent、工具 cwd 与 ephemeral context 精确匹配；持久化 user prompt 不含 workspace block；用户指定的绝对路径原样生效；静默 tick 有结束边界。 |

Agent 实现只触及 `cron/scheduler.py`。`cron/jobs.py`、`tools/cronjob_tools.py`、`cron/execution_workspace.py`、file tools、terminal 和 code execution 均不修改。

Agent 回归测试放在：

- `tests/cron/test_execution_workspace.py`：只补现有 tuple 与 `state.db/cwd` 一致、连续 trigger 不同、fallback 复用的断言；
- 新建 `tests/cron/test_cron_execution_prompt.py`：绝对路径原文保留、token 按本轮 binding 展开、无 binding 时原文保留、user prompt 不含 workspace context、AIAgent ephemeral system context 使用本轮值、wakeAgent=false 的 V1 end_session、provider fallback 失败时只关闭一次 owner session、claim 释放、相邻 job 继续、no-agent 不受影响。

最小实现形状如下：

```python
# cron/scheduler.py
v1_workspace = _prepare_v1_execution_workspace(job)
if v1_workspace is not None:
    _cron_session_id, _job_workdir, _strategy = v1_workspace
else:
    # 保留当前 legacy 初始化逻辑。
    _cron_session_id = (
        str(job.get("_cron_session_id") or "").strip()
        or new_cron_session_id(job_id, now=_hermes_now())
    )
    _job_workdir = str(job.get("workdir") or "").strip() or None

# wake-gate/pre-run script 保持现有顺序；wakeAgent=false 先结束 V1 execution，再静默返回。
prerun_script = _run_job_script_with_claim_heartbeat(job, script_path)
if prerun_script[0] and not _parse_wake_gate(prerun_script[1]):
    _finish_pre_agent_silent(session_db, _cron_session_id)
    return True, _silent_cron_document(job), SILENT_MARKER, None

# execution row 和 _persist_pre_agent_failure() 已建立；只有 gate 通过后才准备 prompt。
prepared_user_prompt = _prepare_cron_user_prompt(
    str(job.get("prompt") or ""),
    execution_session_id=_cron_session_id if v1_workspace else None,
    execution_workspace=_job_workdir if v1_workspace else None,
)

prompt = _build_job_prompt(
    job,
    prerun_script=prerun_script,
    prepared_user_prompt=prepared_user_prompt,
)
```

V1 的 `SessionDB.ensure_session()`、AIAgent 构造和 `TERMINAL_CWD` 必须读取同一组局部值。legacy 分支保留现有 mint/cwd 行为。

窄 renderer 只处理 stored prompt：

```python
def _render_cron_runtime_tokens(prompt, *, session_id=None, workspace=None):
    if not session_id or not workspace:
        return prompt
    return (
        prompt
        .replace("{{cron.session_id}}", session_id)
        .replace("{{cron.workspace}}", workspace)
    )
```

runtime token renderer 仅在两个值都存在时替换 `{{cron.session_id}}` / `{{cron.workspace}}`；没有 V1 binding 时保留 token 原文，不新增配置异常。`_persist_pre_agent_failure()` 只把原 stored prompt 写入该 job 自己的 transcript；日志不需要记录完整 prompt 或路径。未知异常继续走既有通用失败处理。

Agent 测试命令沿用该仓库既有入口；Agent PR 与 WebUI PR 分开提交，端到端验收使用同一 Cron job。

#### 1.4 Cron 继续会话的 workspace 契约

继续某个 Cron 会话不是新的 trigger。WebUI 必须加载该 sidecar，并复用它持久化的 `session_id` 和 `workspace`；不得调用 Agent workspace allocator。

对于 `source_tag=cron` 且 `workspace_mode` 为 `managed` 或 `worktree` 的 V1 session，`workspace_state` 决定继续语义：`ready` 复用持久化 root；`workspace_unverified` 使用批准的 WebUI default workspace；`cleanup_failed` 或未知状态拒绝继续。

`api/workspace.py:resolve_session_workspace()` 是唯一 gate：ready session 没有显式 requested workspace 时返回原 root，requested root 相同则允许、不同则拒绝；unverified session 始终返回批准的 default workspace，不接受调用方用 requested 覆盖；其它状态返回可诊断错误。

继续对话不重新渲染 `{{cron.session_id}}` / `{{cron.workspace}}`。follow-up 只消费 sidecar 已保存的 workspace binding。对正常 `ready` 会话，resolver 的最小检查就是 canonical root 可受信、目录仍存在、requested（若有）与原 root 相同。

这里使用核心 resolver 是必要的最小接缝，因为 browser、gateway 和内部 worker 已共同调用它。若只在 `integration/crons/` 增加检查，会遗漏其它继续会话入口。

这里必须沿用 `cron-session-workspaces.md` 的既有 continuation 契约：`workspace_unverified` 允许继续，但 `resolve_session_workspace()` 固定返回 WebUI 已批准的 default workspace，并明确该目录不是原 execution root。`cleanup_failed`、`workspace_state` 未知、`ready` root 缺失或 ready session 请求不同 root 时，必须阻止 follow-up 并返回可诊断错误。不能把未验证的 sidecar 路径当作 execution root。

`legacy_shared` 与 `workspace_mode=external` 保持当前兼容行为。V1 的 fail-closed 只作用于 cleanup_failed、未知状态、ready root 缺失或 root 替换；workspace_unverified 按既有契约安全降级到 default workspace。

follow-up 开始前，`prepare_cron_session_for_reply()` 仍必须取得 execution 结束边界并完成 settlement。没有结束边界时返回 `unsettled`，不能与仍在运行的原 execution 并发写同一 root。

下一次 scheduler trigger 复用 `job_id`，但创建新的 session ID/root。因此用户继续 execution A 时，即使 execution B 已自动开始，两者也在不同 workspace。

以 managed strategy 为例：

| 动作 | session ID | workspace | `报告.md` 的实际位置 |
| --- | --- | --- | --- |
| 自动 execution A | `cron_job_090000_aaaa` | `/workspace/sessions/cron/default/cron_job_090000_aaaa` | `.../cron_job_090000_aaaa/报告.md` |
| follow-up A | `cron_job_090000_aaaa` | 复用 A | 继续读取或修改 A 的 `报告.md` |
| 自动 execution B | `cron_job_093000_bbbb` | `/workspace/sessions/cron/default/cron_job_093000_bbbb` | `.../cron_job_093000_bbbb/报告.md` |

A 与 B 使用同一相对文件名也不会互相覆盖。只有显式稳定项目路径或 root 外绝对路径可能跨 execution 共享。

runtime token 只属于 stored Cron job prompt。普通 follow-up 不展开 `{{cron.workspace}}`；它通过 session cwd 使用相对路径。

该契约不覆盖 job 显式允许的稳定项目路径，也不覆盖 terminal 主动写出的 root 外绝对路径。这两类路径可能跨 execution 共享，属于明确的非隔离行为。

WebUI 最小代码形状：

```python
# api/workspace.py:resolve_session_workspace()
is_managed = workspace_mode in {"managed", "worktree"}
if source_tag == "cron" and is_managed:
    if workspace_state == "workspace_unverified":
        # Compatibility contract: allow a safe continuation workspace, but do
        # not claim it is the historical execution root.
        from api.config import DEFAULT_WORKSPACE
        return resolve_trusted_workspace(DEFAULT_WORKSPACE)
    if workspace_state != "ready":
        raise ValueError("Cron execution workspace binding is not ready")

if is_managed:
    current_resolved = resolve_trusted_workspace(session.workspace)
    if requested not in (None, ""):
        requested_resolved = resolve_trusted_workspace(requested)
        if requested_resolved != current_resolved:
            raise ValueError("Managed session workspace cannot be changed")
    return current_resolved
```

### 2. 以物化为唯一结算边界

在 `integration/crons/` 新增一个 Cron 专属编排 helper，例如：

```python
settle_materialized_cron_session(session) -> CronManifestSettlement
```

它不重新实现 Artifact 提取或 SQLite 写入；只编排已有能力：

1. 验证 `source_tag == "cron"`，并获取本次 execution 的已验证结束边界；没有边界时返回显式的 `unsettled`，不写 empty marker。
2. 对 execution prefix 做现有 Cron transcript reconcile、内部控制消息规范化与稳定连续 `turn:N` 编号。
3. 先以 `touch_updated_at=False` 保存带稳定 `_turn_key` 的 sidecar。
4. 从 store 读取该 session 的已决定 turn key；只对当前 execution prefix 中尚未决定的 turn 调用共享 `_persist_turn_artifact_paths()`。
5. 每一轮必须得到 `status == "persisted"` 且 key 精确匹配；否则返回失败事实，不能把失败写成 empty decision。

该 helper 放在 `integration/crons/`，并由以下两个接缝共同调用：

- `_materialize_and_settle_cron_session_found()`：包住 raw `_materialize_cron_session_found()`，覆盖 ordinary/fallback、exact-run 和 batch history；
- Cron follow-up prepare gate；避免聊天前和物化后维护两套转录规范化/结算逻辑。

helper 自身不获取锁。materialization wrapper 以 `found.session_id` 获取既有 `_get_session_agent_lock()`，并在同一临界区内完成 raw reconcile/save、重新加载和 settlement。

follow-up 路径继续由聊天调用方持有同一把锁后调用 helper。不得在 helper 内二次获取该 non-reentrant lock。

`materialize_after_cron_run()` 继续负责调度完成后的入口，但它只调用这个统一边界，不再拥有一份隐式的唯一 settlement 行为。

重复进入时由已经决定的 turn key 跳过，不能重复写或改变现有 provenance。

#### 2.1 WebUI：逐文件、逐函数改动

本节是实现约束，不是示意图；下列函数名和调用关系应出现在实际 diff 中。

| 文件 | 具体改动 | 不可接受的替代做法 |
| --- | --- | --- |
| `integration/crons/hooks.py` | 将 `prepare_cron_session_for_reply(session)` 中从 `resolve_cron_execution_ended_at()` 开始，到 `_persist_turn_artifact_paths()` 循环结束的代码抽为 `settle_materialized_cron_session(session) -> CronManifestSettlement`。新 `@dataclass(frozen=True)` 至少含 `status`（`persisted` / `unsettled` / `failed`）、`next_turn_key`、`error_stage`、`settled_turn_keys`。 | 在第二个调用点复制该循环，或以 `ready: bool` 丢失“未结束边界”和“store 失败”的区别。 |
| `integration/crons/hooks.py` | 保留 `CronReplyPreparation` 作为聊天兼容适配层：`prepare_cron_session_for_reply()` 调用上面的 settlement，且只在 `status == "persisted"` 时返回 `ready=True`；其余 status 原样映射为 `ready=False` + `error_stage`。 | 修改普通聊天的 `_persist_turn_artifact_paths()` 契约，或让聊天路径与物化路径各自编号 turn。 |
| `integration/crons/session_bridge.py` | 新增 `_materialize_and_settle_cron_session_found(...) -> Optional[str]`。它按 `found.session_id` 持有 per-session lock，调用 raw materializer，再 `Session.load(sid)` 并 settlement。 | 只修改 `materialize_cron_session()` 的出口，从而遗漏 exact-run 与 batch history。 |
| `integration/crons/session_bridge.py` | 将 raw materializer 的全部四类调用改为 wrapper：ordinary/fallback、普通 state.db 命中、`materialize_cron_session_run()` exact-run、`materialize_cron_sessions_for_runs()` batch history。 | 在 route、history 或 output handler 中分别补 settlement。 |
| `integration/crons/session_bridge.py` | wrapper 在 settlement 为 `unsettled` 或 `failed` 时仍返回已物化的 session ID，并记录结构化结果。 | 因 Artifact store 暂时失败而隐藏已成功 import 的 sidecar。 |
| `integration/crons/hooks.py` | 将 `materialize_after_cron_run()` 中现有的 `Session.load(sid)` + `prepare_cron_session_for_reply(session)` 删除；它只调用已统一的 `materialize_cron_session()`。 | 保留“手动执行额外结算、自动/恢复只物化”的双路径。 |
| `api/workspace.py` | `resolve_session_workspace()` 对 V1 managed/worktree Cron 区分状态：`ready` 只返回持久化 root；`workspace_unverified` 固定返回已批准的 default workspace；`cleanup_failed`、未知状态、ready root 缺失或 requested root 不同均报错。 | 对 unverified 使用其未经证明的 sidecar root，或为 follow-up 重新 allocation。 |
| `api/routes.py` | 删除 `GET /api/session` 在成功 materialize 后额外执行的 `reconcile_cron_session_transcript()` + `save()`；session、history、output 只调用统一 materializer。 | 在 route 中保留第二次无锁 sidecar mutation，或用 route 补丁掩盖问题。 |

`CronManifestSettlement` 的核心伪代码如下；实现可拆为私有函数，但顺序不可改变：

```python
def settle_materialized_cron_session(session) -> CronManifestSettlement:
    if session.source_tag != "cron":
        return CronManifestSettlement("persisted")

    ended_at = resolve_cron_execution_ended_at(session)
    if ended_at is None:
        return CronManifestSettlement("unsettled", error_stage="execution_prefix")
    # 写入 execution 边界；reconcile；规范化 internal message；补稳定 turn:N。
    # 此 save 必须发生在任何 Artifact decision 之前。
    prefix, suffix = cron_execution_prefix_and_suffix(session)
    stamped = _stamp_cron_manifest_turn_keys(normalize_cron_manifest_messages(prefix))
    if not _validate_contiguous_turn_keys(stamped)[0]:
        return CronManifestSettlement("failed", error_stage="turn_keys")
    session.messages = [*stamped, *suffix]
    session.save(touch_updated_at=False)

    decided = load_manifest_decided_turn_keys(session)
    for turn in _message_turns(stamped):
        key = turn["turn_key"]
        if key in decided:
            continue
        result = _persist_turn_artifact_paths(session, key)
        if result.get("status") != "persisted" or result.get("turn_key") != key:
            return CronManifestSettlement("failed", error_stage="artifact_decision")
    return CronManifestSettlement("persisted", ...)
```

这里的 `status` 是 helper 和 `_persist_turn_artifact_paths()` 的**进程内返回字段**，不是 Session JSON 或 `session_manifest.db` 的新列。

它用于阻止调用方把“没有写入 decision”误写成 empty。持久化事实仍是 Manifest store 的 decision/record 行。

materialize wrapper 对 `unsettled` 和 `failed` 只记录 `session_id`、profile、status、`error_stage`、已处理 turn key 数。

日志不得记录 prompt、工具参数、工具输出或文件内容。该 helper 不得吞掉 sidecar 成功物化，也不得把失败转成 empty。

#### 2.2 WebUI：测试文件与断言

| 文件 | 新增回归用例 | 核心断言 |
| --- | --- | --- |
| `integration/tests/crons/test_session_bridge.py` | 分别调用 ordinary、exact-run 和 batch materializer。fixture 使用完成的 user → assistant tool call → tool result → final assistant 链。 | 不经过 hook 或 follow-up，三个入口均产生相同 `turn:1` Artifact decision。 |
| `integration/tests/crons/test_hooks.py` | `settle_materialized_cron_session()` 的无结束边界、store failure、重试和已决定 turn 场景。 | 无边界返回 `unsettled` 且零写入；失败不新增 empty；第二次成功只补缺失 turn，既有 non-empty record 不变。 |
| `integration/tests/crons/test_manifest_turns.py` | 真实 Cron prefix 含 compaction/internal control message 的稳定编号。 | reconcile/normalize 后连续 `turn:N`；每个 key 与 `_persist_turn_artifact_paths()` 返回的 `turn_key` 精确一致。 |
| `tests/test_session_manifest_artifact_persistence.py` | 调用 shared persistence 的契约回归。 | 成功写入返回 `status="persisted"`，而失败不被调用方当成 `empty`。 |
| 路由既有测试文件 | 覆盖 session GET、history、output 三个入口各一次首次物化。 | 三者的 records 完全相同；`GET /api/session/manifest` 本身仍零写入。 |
| `tests/test_session_managed_workspace.py` | V1 Cron ready/unverified/cleanup_failed、requested root 相同或不同、root 已删除。 | ready 只复用原 root 并通过一次 resolver gate；unverified 固定使用批准的 default workspace；cleanup_failed、未知状态和 ready root 缺失 fail closed；legacy external/unverified 仍按原兼容行为。 |

测试先证明 ordinary、exact-run 和 batch materialization 都存在缺失，再加入共享 wrapper 后通过。执行命令为：

```bash
./scripts/test.sh \
  integration/tests/crons/test_session_bridge.py \
  integration/tests/crons/test_hooks.py \
  integration/tests/crons/test_manifest_turns.py \
  tests/test_session_managed_workspace.py \
  tests/test_session_manifest_artifact_persistence.py \
  -q
```

### 3. 明确错误、重试与调用方语义

Cron sidecar 的可读性不能因为 Artifact store 暂时不可用而丢失；但系统也不能把“已物化”误报为“已结算”。因此 helper 返回区分：

| 结果 | sidecar | Artifact store | 调用方行为 |
| --- | --- | --- | --- |
| `persisted` | 已保存 | execution prefix 的每个 turn 有非空或 empty decision | 正常返回 |
| `unsettled` | 已保存 | 不新增 decision | 记录原因为未获得 execution 结束边界；下次明确 materialize/retry 可继续 |
| `failed` | 已保存 | 保留已有 rows；不写伪 empty marker | 记录结构化阶段与异常类别，进入显式 repair 队列/维护清单 |

不把 store 暂时故障改写成 Cron run 的模型失败，也不删除已写成果。

失败日志只含 `session_id`、Profile、turn key、阶段和错误类别。不得包含 prompt、文件内容、凭据或完整工具结果。

对 `GET /api/session` 的“首次 materialize”分支，物化本来就是写 sidecar 的恢复操作，因此允许在该流程中结算缺失 decision。

`GET /api/session/manifest` 仍仅投影现有 store，不调用这个 helper。

### 4. 显式历史迁移与修复

#### 4.1 审计包含旧 session root 的 job（可选）

由于产品允许用户在 prompt 中指定具体目录，不要求暂停或迁移这些 job。仍可用只读 audit 列出 concrete execution paths，供运维确认哪些任务会把文件写到当前 execution workspace 之外；audit 只输出 job ID、名称、命中类型和行号，不输出完整 prompt。

受影响的 `d0578e1b0766` 可以按业务需要更新。若希望输出跟随每轮 execution workspace，可将两条 `<workspace>/sessions/bbbad785a663/...` 改为：

```text
完整版 md 使用相对文件名：今日AI热点简报_YYYY-MM-DD_HHMM.md。
回复中的 MEDIA 路径使用 write_file 返回的 resolved_path。
```

相对路径仍是推荐写法，token 是需要显式绝对路径时的可选写法。更新不得修改历史 transcript，也不移动已生成文件。

若其他 job 确实需要在 prompt 中展示绝对 root，可改为 `{{cron.workspace}}/...`。语义不明确的路径只报告，不自动改写。

是否更新旧路径由用户决定；无论是否更新，第二次 trigger 都必须生成新的 session ID/root。旧路径若仍保留，会按用户原意执行，并可能跨 execution 共享文件。

#### 4.2 修复缺失的 Artifact decision

新增维护脚本（或等价的仅管理员可调用维护入口）以修复已受影响的 Cron sessions：

```text
scripts/repair_cron_manifest_artifacts.py --profile <profile> --session-id <cron-id> --dry-run
scripts/repair_cron_manifest_artifacts.py --profile <profile> --session-id <cron-id> --apply
```

首批 exact-session 范围固定为：

- `cron_4f2f2bad65ab_20260829_083013_3a36a484`
- `cron_d0578e1b0766_20260829_164234_0a0d911e`

执行前必须从其 durable `state.db` row 验证 owner Profile，不能根据当前 active Profile 猜测。

约束：

- 默认 `--dry-run`，输出每个 turn 的 `existing / candidate / skipped / failed` 计数，不输出文件内容。
- `--apply` 只接受精确 session ID；批量模式必须要求显式 `--all-missing` 和受限 Profile，并先输出计划。
- lineage 完全没有 decision 时调用现有 missing-records backfill；只有 empty decision 的 turn 才可调用 empty repair；non-empty decision 绝不覆盖。
- 使用同一个 Cron settlement helper，因而沿用 transcript、turn、路径和 preview gate；不得写另一个“历史解析器”。
- 不移动、复制或删除文件。历史上已成功写到当前 Artifact root 内、且有强工具证据的文件可登记为原路径；根外或不安全路径按既有 policy 跳过并报告。

这使已经发生的“会话可见但没有 Artifact”能够被修复，同时不把普通用户浏览变成隐式数据库写入。

### 5. 文档与接缝归属

| 位置 | 职责 |
| --- | --- |
| Agent `cron/scheduler.py` | 解包既有 V1 tuple，将相同 session/workspace 局部值传给 SessionDB、AIAgent、cwd 与 prompt builder |
| `integration/crons/` | Cron materialization、execution-prefix settlement 与历史 repair 编排 |
| `integration/session_manifest/` | 保持现有候选提取、preview gate、store 与 repair primitives；不增加 Cron 特例 |
| `api/streaming.py` / `api/gateway_chat.py` | 继续作为普通 browser/gateway turn 的共享 settlement 接缝；不复制 Cron policy |
| `api/workspace.py` | 继续会话的唯一 workspace gate；V1 managed/worktree 按状态复用 ready root 或降级到批准的 default，危险状态拒绝 |
| `api/routes.py` | 只调用 Cron materialization/repair 的薄入口，不自行扫描 transcript 或写 Artifact rows |

实现时更新：

- `docs/rfcs/cron-session-workspaces.md`：交叉引用本 RFC 的 V1 tuple prompt binding、continuation reuse 与 settlement，不改变既有 allocation 契约；
- `docs/architecture/session-manifest-artifacts.md`：补充 Cron materialization settlement 的生命周期入口；
- `integration/README.md` 与 `docs/hermes-external-integration.md`：记录新增的 `integration/crons/` helper 和最小上游接缝；
- 若维护入口是 HTTP API，才同步 `integration/swagger/openapi.json` 与路由表；优先脚本，避免为一次性 repair 新增公共 API。

## 实施顺序

1. **冻结回归 fixture**：保存无敏感内容的 stale-prompt、成功 `terminal` 和成功 `write_file` fixtures。先证明旧 prompt 跨轮保留，且 ordinary、exact-run、batch materialization 都缺少 decision。
2. **Agent 最小 prompt 接缝**：保留 user prompt 原文，仅展开可选 runtime token；将当前 execution 的 workspace context 放入 `ephemeral_system_prompt`，先证明用户指定的绝对路径不被改写。
3. **WebUI settlement 边界**：抽取 settlement helper，并以 `_materialize_and_settle_cron_session_found()` 替换 raw materializer 的全部四类调用；删除 scheduler hook 的重复结算。
4. **Continuation workspace gate**：V1 managed/worktree 的 ready 复用原 root，workspace_unverified 降级到批准的 default，危险状态拒绝；增加继续 A 与自动 B 并发、root 不同的回归测试。
5. **可选 prompt 整理**：根据业务需要将部分固定目录改成相对路径或 token；不作为部署门禁。
6. **历史 Artifact repair**：实现 exact-session dry-run/apply，先修复两个已知 session；无人工确认不执行批量 apply。
7. **联合验收**：自动和手动各运行一次同一 job，继续旧 execution，再核对 tuple、workspace、Manifest DB、GET 与 preview。

Agent prompt 接缝、WebUI settlement、continuation gate、历史 repair 应分别提交。跨仓库 PR 可独立回滚。

## 测试矩阵

| 场景 | 必须断言 |
| --- | --- |
| 同一 job 连续两个自动 trigger | 产生不同 `session_id` 与 workspace；每轮的 tuple、`state.db.id/cwd`、AIAgent、sidecar 精确匹配 |
| runtime token | `jobs.json` 保持 token 原文；每轮 assembled prompt 只出现本轮值，不含上一轮 root |
| skill/script/context 注入 | 外部内容中的同名 token 不展开；只渲染 stored user prompt |
| concrete session root | create/update/trigger 均允许；按用户原意执行，可能跨 execution 共享文件；不自动改写；有合格成果证据时按既有 Artifact gate 结算 |
| prompt 路径形式 | 不影响 create/update/pause/disable/delete/resume/trigger；scheduler 不因路径形式生成 failure tuple |
| V1 wakeAgent=false | 不展开 prompt、不启动 AIAgent；已创建的 V1 `state.db` execution 有 `cron_complete` 结束边界 |
| provider fallback 配置失败 | primary/fallback 共用一个 session/root；失败只写一次终态，不留下 active session 或重复 user prompt |
| 同 cycle 的相邻正常 job | 其它 job 因脚本/provider/Agent 失败后，正常 job 仍执行成功，状态与输出不受污染 |
| no-agent script job | prompt 不参与；script path 校验、wake gate 和执行语义保持现状 |
| 相对路径与稳定项目路径 | 继续保存并相对本轮 cwd 执行；legacy 无 workspace policy 的 job 行为不变 |
| pinned-provider global fallback | 复用同一 transient session ID、state.db session 和 root，不产生第二个 execution workspace |
| 继续 execution A | follow-up 的 session ID、sidecar workspace 与 cwd 均保持 A；不调用 allocator，也不展开 Cron runtime token |
| ready follow-up 最小 gate | 只校验 canonical root、目录存在性和 requested 等值；不重新分配 workspace |
| 继续 A 时自动触发 B | B 获得新 session ID/root；A、B 使用相同相对文件名也不会覆盖彼此 workspace 内的文件 |
| V1 workspace binding 非 ready | `workspace_unverified` 允许继续但固定使用批准的 default workspace；`cleanup_failed`、未知状态和 ready root 缺失阻止 follow-up |
| legacy Cron continuation | `external/legacy_shared` 继续保持现有共享 workspace 兼容语义 |
| requested continuation workspace | ready 且等于持久化 root 时允许；ready 的不同 root 在 chat/tool 启动前拒绝；unverified 忽略 requested 并固定使用批准的 default |
| scheduler callback materialization | 每个真实 turn 仅有一个 stable decision；重复 callback 不重复写 row |
| 直接 `GET /api/session` 恢复 | 首次 sidecar import 后已有 Artifact decision；不必先发送 follow-up |
| ordinary/exact-run/batch materialization | 与 scheduler callback 得到相同 manifest rows，且没有 raw materializer 漏网调用点 |
| 成功 `write_file` | 参数与成功结果按同一 call ID 配对，保存的 `source_tool` 为 `write_file` |
| 成功 terminal + 最终回复路径 | 合法最终交付路径在文件存在时持久化；stdout 本身仍不成为 Artifact |
| 无成果的成功 Cron turn | 仅写一个 empty marker；wire `artifacts` 为空 |
| store 写入失败 | sidecar/transcript 保留、无伪 empty marker、失败可被 exact-session repair 重试 |
| 既有 non-empty decision | direct materialization 不重写 path、source_tool、profile 或 workspace_root |
| `GET /api/session/manifest` | 对缺失/empty/non-empty records 均零写入 |
| 并发 history 与 session recovery | 不出现跨 turn、跨 Profile、跨 workspace root 的重复或串写 |
| materialization 与 follow-up 并发 | 同一 session 的 sidecar reconcile/save/settlement 串行；helper 不二次获取 non-reentrant lock |
| 历史 repair dry-run/apply | dry-run 零写入；apply 仅修改目标 session 的 DB rows，第二次 apply 幂等 |

WebUI 测试通过 `./scripts/test.sh` 运行。Agent PR 按 Agent 仓库既有入口运行第 1.3 节测试；联合验收必须记录两侧 commit/version。

## 发布、观测与回滚

### 上线顺序

1. 先部署 WebUI settlement PR。它不依赖 Agent prompt 接缝，可立即修复自动 materialization 的新 Artifact decision。
2. 审计 V1 sidecar 的 workspace state，修复可验证 binding 后部署 continuation gate；unverified session 保持阻止状态。
3. 部署 Agent prompt binding PR；它只注入 ephemeral workspace context、展开可选 token，不改变现有 prompt 的可执行范围。
4. 新自动 run、follow-up 和下一轮自动 run 验收通过后，再执行历史 Artifact exact-session repair。

### 发布前

- 记录运行中 WebUI 与 Agent 的版本、启动时间和执行 Cron scheduler 的进程归属；仅检查非敏感元数据。
- 可选地对 `d0578e1b0766` 及其它 job 做只读 concrete session root 审计；保存 job ID、名称、命中类型和行号，不作为上线前阻断条件。
- 审计 V1 Cron sidecar 的 `workspace_mode/state` 和 root 可用性；只输出 session ID、状态和错误类别。
- 对需要跟随每轮 workspace 的 prompt，可选改成相对路径或 runtime token，并验证相对路径仍落在当前 `TERMINAL_CWD`；明确保留的绝对路径也要验证按用户原意执行。
- 用隔离 fixture 验证 prompt 原文保存、runtime claim 释放和同 cycle 正常 job。
- 先运行 exact-session dry-run，验证候选 path、source tool、turn key 和 workspace root。
- 用一个新的自动 Cron execution 和一个手动 Cron execution 进行对照验证。

### 发布后验收

对一个新 Cron run 验证同一组事实：

```text
stored job: semantic task + user-selected path (relative or absolute)
  -> existing V1 tuple(session_id, workspace_root, strategy)
  -> Agent state.db(id, cwd) + transcript
  -> WebUI sidecar(session_id, workspace) with stable turn key
  -> session_manifest.db non-empty/empty decision
  -> GET /api/session/manifest
  -> Artifact chip / Inspector preview
```

随后继续该 execution A，确认 cwd 仍是 A；再触发 execution B，确认 B 使用新 root。A、B 分别写同名相对文件时，两侧文件内容与路径必须独立。

观测记录只统计 prompt binding、claim release、continuation gate、Manifest settlement，以及外部直接引用候选的接受/拒绝数量。

日志不得记录完整 prompt、渲染后的绝对路径、文件内容或用户数据；只保留 job ID、阶段和结构化错误类别。

告警条件包括 scheduler loop/claim 异常、正常 job 被连带跳过、prompt/settlement 连续失败、ready root 不可用和完成后仍有 `unsettled` turn。

### 回滚

- 回滚 Agent prompt binding 不回写 `jobs.json`，已有绝对路径继续按旧版本语义执行；不改变已生成的 execution identity。
- 回滚 continuation gate 只恢复旧 fallback 行为，不修改 sidecar workspace/state；回滚前记录被阻止 session 范围。
- 回滚 WebUI 代码只停止新的统一结算；不删除任何已写的 Manifest row、sidecar 或工作区文件。
- 历史 repair 不可通过回滚自动撤销；它只新增或替换空 decision，执行前必须保留 dry-run 输出与 session 范围记录。

## 已决观测策略

V1 不新增 settlement failure ledger。使用结构化日志、运行历史中的失败 execution 和 exact-session repair 清单。

只有在生产证据证明进程重启会让失败 session 无法枚举或重试时，才另行设计持久化 ledger；不得把该扩展塞入本次修复。
