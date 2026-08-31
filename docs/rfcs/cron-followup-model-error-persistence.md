# Cron Follow-up 占位模型与终止错误保留修复

- **Status:** Proposed
- **Author:** Hermes WebUI maintainers
- **Created:** 2026-08-30

## 摘要

本 RFC 修复 Cron 会话 follow-up 中两个已经由真实会话证据确认的问题：

1. no-agent Cron 会话物化时使用 `model="unknown"` 表示“本轮没有模型”。新会话因此从创建起就携带无效模型；共享 resolver 又把该占位值当作可执行模型，并在已有 `model_provider="custom"` 时通过 fast path 原样返回，最终向上游发送 `model=unknown`。
2. 第一次 follow-up 的 provider 错误已经成功写入 WebUI sidecar 和 run journal，但第二次 follow-up 启动后，一个缺少该错误的 Session 快照覆盖了当前 sidecar。`.json.bak` 仍保留第一次错误，当前 sidecar 只保留第二次错误。

这两个问题分别属于模型值规范化和 Session 快照一致性。修复必须使用现有共享接缝：模型占位值在共享 resolver 中处理；终止错误在同一 session lock 下随完整 transcript 保留。

本 RFC 不新增数据库表，也不把 WebUI 合成错误写入 Agent `state.db.messages`。错误气泡以 WebUI sidecar 为可见 transcript 的权威记录，run journal 保存对应流的终止事件；修复重点是保证这两份既有持久化数据不被旧 Session 快照覆盖。

## 真实复现时间线

受影响 session：

```text
cron_3eef54bb00a4_20260830_154600_2a19f60b
```

两条内容相同的“文件呢”是两次真实用户提交，不是同一次提交的重复恢复：

| 时间 | stream | 事件 | 持久化结果 |
| --- | --- | --- | --- |
| `1788075985.447` | `167503636ea44f0c9107d53918d7f7a2` | 第一次 follow-up：“文件呢” | Agent `state.db` 有用户行；run journal 有 terminal `apperror`；第一次 assistant 错误进入 sidecar |
| `1788075995` | 同上 | 三次 `model=unknown` 请求后失败 | 该错误现存于 `.json.bak` |
| 约 `1788076015` | `3a8f9756e92d48b9b4e87b4f2f8ceb1c` | 第二次 follow-up：“文件呢” | Agent `state.db` 有第二条用户行；第二个 run journal 独立存在 |
| `1788076025` | 同上 | 再次三次失败 | 当前 sidecar 只保留该次 assistant 错误 |

因此正确的最终 transcript 应是：

```text
turn:1  cron prompt
        cron result
turn:2  文件呢
        error(stream A)
turn:3  文件呢
        error(stream B)
```

不能按内容合并两个用户 turn，也不能只保留最后一条错误。

## 根因

### 1. `unknown` 占位值绕过共享模型回退

`integration/crons/session_bridge.py` 在导入没有推理模型的 no-agent Cron execution 时写入 `model or "unknown"`。这个值表达“该 execution 没有模型”，不是 Provider 可执行的模型 ID。

因此问题有两个接缝：物化层不应继续制造占位模型；共享 resolver 仍需兼容已经落盘的历史 `unknown`。只修 resolver 会让后续新建会话继续携带脏数据，只修物化层则不能恢复旧会话。

`api/routes.py:_resolve_compatible_session_model_state()` 当前先执行 model + provider fast path。只要 session 同时携带 `model="unknown"` 与 `model_provider="custom"`，resolver 就原样返回，profile 默认模型分支无法运行。

错误值链路为：

```text
no-agent execution 没有模型
  -> materialize 写入占位值 "unknown"
  -> session provider 被解析为 custom
  -> shared resolver fast path 原样返回 unknown
  -> chat-start / streaming worker 使用 unknown
  -> Provider 返回 503 model_not_found
```

这个故障类别不限于 Cron。任何 imported/foreign session 如果使用同一占位值并带显式 provider，都可能命中相同 fast path。因此根修复必须位于共享 resolver，Cron integration 只负责提供复现测试。

### 2. 已保存错误被后续 Session 快照覆盖

第一次错误同时出现在对应 run journal 和 `.json.bak`，证明错误构造、`Session.save()` 和 journal append 都曾成功。当前 sidecar 缺少第一次错误，说明后续完整 Session 保存使用了不包含该 terminal row 的旧快照。

高风险边界包括：

- `get_session()` 的 LRU cache 与另一个 `Session.load()` 返回的 sibling object；
- `prepare_cron_session_for_reply()` / Cron materialization 在锁内加载并保存新对象，但未同步或失效内存 cache；
- `_start_chat_stream_for_session()` 接收锁外解析出的 Session 对象，在取得 session lock 后直接调用 `_prepare_chat_start_session_for_stream()` 保存；
- state.db reconciliation 只拥有 Agent transcript，不能凭较新的用户行证明 WebUI sidecar-only error 已不存在。

`.json.bak` 由“incoming message count 小于现有 sidecar”保护逻辑产生，也说明发生过 transcript shrink。修复必须先用确定性测试钉住具体 shrink writer，然后在该共享接缝消除旧快照写入；不能只在读取时从 journal 反复补回。

## 目标

1. `unknown`、空字符串和其它明确的内部占位模型永远不能进入 Provider 请求。
2. 后续新建的 no-agent Cron sidecar 在物化时绑定所属 Profile 的完整默认 model/provider，不再持久化字符串 `unknown`。
3. 共享 resolver 将历史占位模型按“未设置模型”处理，并使用 session 所属 profile 的完整默认 model/provider。
4. profile 没有可验证默认模型时，在创建 worker 和发出网络请求前明确失败。
5. chat-start 使用 session lock 内重新确认的最新完整 Session 作为 transcript 写入对象。
6. Cron materialization、reconciliation、GET 投影和下一次 chat-start 都保留已落盘的 WebUI terminal error。
7. 两个内容相同但属于不同 turn 的用户消息和错误消息都必须按 transcript 顺序保留。
8. sidecar 保存失败不再静默；日志能定位 session、stream、turn、阶段和异常类型。
9. 不新增数据库表；Agent `state.db` 的所有权和既有 schema 保持不变。
10. WebUI 合成的 provider error 不写入 `state.db.messages`，但必须可靠保存在 sidecar，并在 run journal 中留下对应 terminal event。

## 非目标

- 不修改 Cron job 的固定 Provider/model 绑定契约；该主题由 `cron-model-provider-binding.md` 负责。
- 不从 model 名称猜测 Provider。
- 不从当前 Cron job 配置重建历史 execution 的模型绑定；job 可能已在 execution 完成后被编辑。
- 不新增 Agent inference-binding API、execution ledger 或 `EffectiveCronModel` 数据类型。
- 不重写整个 Session cache、run journal 或 streaming 协议。
- 不把 run journal 变成普通 session GET 的主数据源。
- 不新增 per-turn outcome、session run 或 error 数据库表。
- 不复用 `state.db.messages`、`sessions.end_reason` 或其它现有字段存放 WebUI 合成错误。
- 不在本 RFC 中实现 Artifact/Manifest 结算。
- 不以内容相同或“时间接近”为理由合并真实用户 turn。

## 状态所有权与不变量

| 状态 | 所有者 | 本次修复后的契约 |
| --- | --- | --- |
| Cron execution transcript | Agent `state.db` | 保存 Agent 实际提交的 transcript 行；schema 不变，不保存 WebUI 外层合成错误 |
| 可见完整 transcript | WebUI sidecar | 保存 Agent prefix、WebUI follow-up 和每个 terminal error；后续写入只能追加或有证据地替换 |
| turn 生命周期 | WebUI turn journal | 保存 `turn_key`、`stream_id`、`turn_id` 与 submitted/terminal lifecycle 的关联 |
| live/replay 事件 | WebUI run journal | 按 stream 保存 SSE 观察和 terminal state；用于重连、审计及既有恢复流程 |
| 当前有效模型 | chat-start shared resolver | 输入是 session model/provider 与 profile 配置；输出值必须与 worker 实际使用值一致 |
| 活跃 turn identity | sidecar pending fields + user turn key；turn/run journal 持有 stream id | 用户消息与紧随其后的错误按 transcript 顺序关联；执行流通过 journals 关联 |
| Session 内存对象 | `SESSIONS` cache + per-session lock | 锁内写入前必须确认对象没有落后于最新 sidecar |

必须成立：

1. Provider 收到的 model 不是空值、`unknown` 或内部占位值。
2. resolver 决定的 model/provider 与 `_prepare_chat_start_session_for_stream()` 保存、worker 注册和 Agent 构造使用同一组值。
3. no-agent Cron 的新 sidecar 在首次 follow-up 前已经保存完整、可执行的 Profile 默认 model/provider；execution 审计模型仍由 Agent/Cron execution record 表达。
4. 已成功写入 sidecar 的 terminal row 不会因 state.db 缺少对应行而被删除。
5. 一个真实用户提交对应一个唯一 turn key；相同文本的下一次提交得到下一个 turn key。
6. terminal error 按所在 user turn 后的 transcript 位置保留；不得因文案或错误类型相同而跨 turn 合并。
7. sidecar 写入和 journal 写入都可能因存储故障失败。代码保证逐项尝试并记录结果，但不承诺磁盘故障时至少一个一定成功。

### 为什么不写入现有 `state.db`

该 503 虽然发生在一次会话 follow-up 中，但用户看到的中文错误行是在异常越过 Agent 运行边界后，由 WebUI streaming 外层合成。它不是模型生成的 assistant 回复，也不是 Agent 已提交的 transcript message。

当前 `state.db` 没有每轮运行终态结构。`sessions.end_reason` 属于整个 session，无法表示多个 follow-up；`messages` 又缺少 `turn_key`、`stream_id` 和结构化错误字段。

把错误强行写成普通 assistant message 会产生三个问题：下一次模型可能把它当作历史回答；sidecar/state.db reconciliation 可能重复显示；WebUI 会成为 Agent 数据库的额外 writer。

因此在“不新增数据库表”的约束下，存储边界确定为：`state.db` 不变，sidecar 按 transcript 顺序保存可见错误，run journal 保存对应 stream 的终止事件，turn journal 保存 turn 与 stream 的关联。

这不是不持久化错误。sidecar 和 run journal 都是磁盘持久化层；本次缺失的根因是后续旧快照覆盖 sidecar，而不是错误从未落盘。

## 修复方案

### 1. Cron 新会话不再写入 `unknown`

在 `integration/crons/session_bridge.py:_materialize_cron_session_found()` 区分“execution 实际模型”和“WebUI continuation 模型”。Agent execution 有真实 model 时原样保存；no-agent execution 没有模型时，读取 `target_profile` 的完整默认 model/provider：

```python
execution_model = str(model or "").strip()
session_model = execution_model
session_provider = None
if job.get("no_agent") is True and not execution_model:
    session_model, session_provider = read_profile_default_binding(
        target_profile,
    )

s = import_cli_session(
    ...,
    model=session_model,
    model_provider=session_provider,
)
```

这里的 `Session.model` 表示该 WebUI 会话下一次 turn 的模型绑定，不是 execution 审计字段。no-agent execution 没有调用 LLM这一事实继续由 Agent/Cron execution record 表达，不通过 `Session.model` 推断。

Profile 默认绑定必须作为 model/provider 整体读取并保存，不能只填 model 后沿用环境中的 provider。优先复用 `integration/crons/execution_model.py` 已有的 Profile 配置读取逻辑，不从模型名称猜 Provider，也不在物化阶段触发 live catalog 网络发现。

如果 Profile 没有完整、可验证的默认绑定，sidecar 保存空 model/provider，并由第 3 节的运行前 gate fail closed；不得为了消除 `unknown` 而选择目录中的任意模型。

已有 sidecar 的更新规则保持保守：只有 sidecar 仍为空或为旧 `unknown` 时才补齐绑定。已经由用户选择或 WebUI follow-up 保存的实际 model/provider，不得被 Cron 重放、Profile 后续变更或 reconciliation 覆盖。

为保证 model/provider 原子进入新 sidecar，给 `api.models.import_cli_session()` 增加向后兼容的可选 `model_provider=None` 参数，并直接传给 `Session(...)`。不修改该函数的其它调用语义，也不改变普通 CLI、消息平台和其它 foreign session 的默认行为。

具体修改：

1. 在 `integration/crons/execution_model.py` 新增 `read_profile_default_binding(profile: str) -> tuple[str, str | None]`。它只读取目标 Profile 的 `config.yaml`（必要时读取该 Profile 的运行环境），不调用 `_discover_first_inference()`，不触发 live catalog 网络请求；返回空 model 表示没有可绑定默认。
2. 在 `session_bridge.py` 将 `found` 中的 execution model 保留为审计输入，另计算 `session_model/session_provider`。不要把 `found[3]` 直接当作 no-agent 会话模型。
3. 修改新建分支的 `import_cli_session(...)` 调用，显式传 `model=session_model`、`model_provider=session_provider`。
4. 修改已有 sidecar 分支的 `needs_model_update`：只在 sidecar 模型为空或为历史 `unknown` 时补齐；增加 provider 一致性判断，但如果 sidecar 已有用户选择或已启动 follow-up 的绑定，不得覆盖。
5. 在 `api/models.py:import_cli_session()` 的参数末尾增加 `model_provider=None`，不要插入到 `model` 和 `profile` 之间，以免破坏现有位置参数调用；构造 `Session(...)` 时传入该值。

签名保持现有位置参数顺序，仅在末尾追加：

```python
def import_cli_session(
    session_id, title, messages, model='unknown', profile=None,
    created_at=None, updated_at=None, parent_session_id=None,
    workspace=None, workspace_mode=None, workspace_state=None,
    require_workspace_binding=False, model_provider=None,
):
    s = Session(..., model=model, model_provider=model_provider, ...)
```

`read_profile_default_binding()` 应复用现有配置读取 helper，而不是重新解析模型目录：

```python
def read_profile_default_binding(profile: str) -> tuple[str, str | None]:
    from api.profiles import get_hermes_home_for_profile

    home = get_hermes_home_for_profile(profile)
    model, provider = _configured_inference(home)
    if not model:
        model = _environment_model(home)
    return str(model or '').strip(), str(provider or '').strip() or None
```

如果产品不希望环境变量影响会话绑定，则去掉 `_environment_model()` fallback，并将配置文件未设置默认模型视为 unresolved。两种行为必须在测试中固定，不能退回 `_discover_first_inference()` 的任意首个目录模型。

推荐的物化局部结构如下，`execution_model` 不写入 Session 时不得丢失，它仍用于 execution 读取和日志：

```python
execution_model = str(model or '').strip()
session_model = execution_model
session_provider = None
if job.get('no_agent') is True and not execution_model:
    session_model, session_provider = read_profile_default_binding(target_profile)

import_cli_session(
    sid, title, msgs,
    model=session_model,
    model_provider=session_provider,
    profile=target_profile,
    ...,
)
```

如果 Profile 默认只有 model、没有显式 provider，允许 `session_provider=None`，由现有 resolver 按 Profile/Provider 配置解析；禁止填入 `custom` 或 `unknown` 作为猜测值。

### 2. 在共享 resolver 规范化历史占位模型

在 `api/routes.py:_resolve_compatible_session_model_state()` 的任何 model/provider fast path 之前，增加单一占位值规范化 helper，例如：

```python
def _is_unresolved_session_model(value: object) -> bool:
    model = str(value or "").strip().lower()
    if not model:
        return True
    if model == "unknown":
        return True
    if model.startswith("@") and model.endswith(":unknown"):
        return True
    return False
```

resolver 将占位模型转为空模型，再进入现有 profile-aware fallback。不能在 `integration/crons/` 复制一套模型目录或 Provider 推断器。

当占位模型同时携带历史 `model_provider` 时，若 `_read_profile_model_config()` 已确认该 provider 与 Profile provider 匹配，应优先返回 Profile 的完整默认 model/provider，不进入 live catalog fast path。

具体修改 `api/routes.py:_resolve_compatible_session_model_state()`：

```python
unresolved = _is_unresolved_session_model(model_id)
model = '' if unresolved else str(model_id or '').strip()
requested_provider = _clean_session_model_provider(model_provider)

if unresolved and explicit_model_pick:
    # 显式选择了无效占位值，不能静默改成默认模型。
    return '', requested_provider, True
if unresolved:
    # 历史导入的 provider 也可能是 unknown/custom，占位模型时不能走 fast path。
    requested_provider = None
```

该判断必须位于现有 `if model and requested_provider` fast path 之前。之后沿用已有 profile-aware fallback：优先使用 `_read_profile_model_config(session, ...)` 提供的 Profile 默认，解析不到时返回空 model，由运行前 gate 拒绝。显式有效 model/provider 的 fast path 不变。

`_is_unresolved_session_model()` 只接受明确的内部占位形式：空字符串、`unknown`、大小写变体和已知的 `@provider:unknown` 形式；不把任意包含 `unknown` 的真实供应商模型名误判为占位值。

解析顺序保持现有会话语义：

1. 请求中明确选择且通过现有校验的 model/provider；
2. session 中有效的 model/provider；
3. session 所属 profile 的完整默认 model/provider；
4. 无法解析时返回 unresolved，由运行前 gate 拒绝。

对于历史 `unknown` no-agent 会话，不读取当前 job binding。它在创建 execution 时没有调用模型，follow-up 应回退到 session 所属 Profile 的完整默认绑定；第一次成功解析后，现有 `_prepare_chat_start_session_for_stream()` 会把实际 model/provider 原子保存到该 session。

### 3. 在共享运行前 gate 拒绝 unresolved 模型

resolver 规范化后仍可能遇到 profile 默认模型为空或配置不完整。所有会进入 `_start_chat_stream_for_session()` 的入口都必须经过同一个网络前 gate：

```text
resolved model 非空
AND model 不是内部占位值
AND provider/model 组合通过现有配置校验
```

失败时：

- 不写 `active_stream_id`；
- 不创建 streaming worker；
- 不构造 AIAgent；
- 不发 Provider 请求；
- 返回稳定错误类型 `session_model_unresolved`；
- Cron follow-up 显示中文错误：“当前会话没有可用模型，请配置当前 Profile 的默认模型或重新选择模型。”

普通 chat-start、process wakeup、goal kickoff 等共享入口也得到同一保护，避免相邻路径继续发送占位值。

运行前 gate 的伪代码：

```python
def _validate_start_model(model, provider):
    if _is_unresolved_session_model(model):
        return {
            'error': '当前会话没有可用模型，请配置当前 Profile 的默认模型或重新选择模型',
            'type': 'session_model_unresolved',
            '_status': 409,
        }
    return None
```

`_start_run()` 和 `_start_chat_stream_for_session()` 都调用它，但错误 payload 只在最外层返回一次；不要在 gate 中写 Session、创建 stream 或追加 journal。

具体修改：

1. 在 `api/routes.py` 增加小型纯函数 `_validate_start_model(model, model_provider) -> dict | None`，检查空值、占位值和现有 provider/model 配置校验；返回值为稳定错误 payload 或 `None`。
2. 在 `_start_chat_stream_for_session()` 的任何 `Session` pending 字段写入、`active_stream_id` 设置和 worker 创建之前调用该函数。
3. 在 `_start_run()` 的 adapter 分支之前也调用同一 gate，防止 runtime adapter 或直接测试调用绕过 legacy streaming path。
4. gate 失败返回 `{"error": "当前会话没有可用模型，请配置当前 Profile 的默认模型或重新选择模型", "type": "session_model_unresolved", "_status": 409}`；不能设置 `active_stream_id`、`pending_user_message`，不能创建 SSE channel 或 Agent worker。

### 4. 先用失败测试定位 transcript shrink writer

在修改错误恢复逻辑前，使用真实复现形状建立确定性测试。测试必须包含两个相同文本的独立提交和两个 provider failure：

```text
initial cron prefix
send "文件呢" -> stream A -> error A persisted
materialize/reconcile/session GET
send "文件呢" -> stream B -> error B persisted
reload sidecar
```

未修复版本必须得到“error A 消失或 turn key 冲突”的失败结果。测试中保留 sibling Session/cache 条件，不能只调用纯消息 merge helper。

若最小测试尚不能复现，在以下边界加入临时、带唯一前缀的诊断信息：

- `Session.save()`：session ID、Python object ID、incoming/disk message count、最后 user/error 的 turn identity；
- `get_session()` cache hit/reload；
- Cron materialize/settlement 的 load/save；
- `_start_chat_stream_for_session()` 取得锁前后使用的 Session object ID。

定位后删除临时诊断，再实施下面的锁内刷新规则。

代码上不要直接修改多个保存调用点来“补消息”。先把测试失败时的 writer 定位到以下三个调用点之一：`_materialize_cron_session_found()` 的 `full.save()`、`_materialize_and_settle_cron_session_found()` 的 settlement save，或 `_prepare_chat_start_session_for_stream()` 的 pending save。测试必须记录写入前后的 sidecar `message_count`、最后一条消息的 role/content 摘要和对象身份，才能证明是哪一个旧对象造成缩短。

### 5. chat-start 锁内刷新最新完整 Session

`_start_chat_stream_for_session()` 已拥有 per-session lock，但当前传入的 `s` 可能是在锁外从 cache 取得的旧对象。取得锁且确认没有 live owner 后，应在写 pending state前重新确认最新完整 sidecar：

1. 在 session lock 内按精确 session ID 执行 `Session.load()`；
2. 验证加载对象不是 metadata-only stub，且 session/profile/workspace identity 与请求目标一致；
3. 若磁盘对象包含 candidate 没有的 settled user/assistant/error rows，采用磁盘对象；
4. 只通过现有 append-only reconciler 合并较新的 Agent state.db rows，不因 state.db 缺行删除 sidecar-only terminal rows；
5. 将最终对象写回 `SESSIONS[sid]`，后续 pending save 和 worker 使用同一个对象；
6. 基于刷新后的 transcript 重新计算/校验 next turn key；若调用方准备的 key 已冲突，重新绑定到正确 next key 或 fail closed，不能复用旧 key。

刷新必须保持已验证的 workspace/model 决策与动作一致。若锁内最新 session 的 profile、workspace binding 或显式 model binding 与锁外解析依据不同，应重新执行相应 resolver，不能用旧值覆盖最新配置。

建议在 `api/routes.py` 增加一个只供启动路径使用的 helper：

```python
def _reload_session_for_locked_start(s):
    sid = str(getattr(s, 'session_id', '') or '').strip()
    latest = Session.load(sid)
    if latest is None:
        raise KeyError(sid)
    if getattr(latest, '_loaded_metadata_only', False):
        raise RuntimeError('locked chat start requires a full session')
    with LOCK:
        SESSIONS[sid] = latest
        SESSIONS.move_to_end(sid)
    return latest
```

在 `_start_chat_stream_for_session()` 的 `with session_lock:` 第一段、检查 `active_stream_id` 之前调用它，并将后续所有 `s.*` 读写统一改为返回的 `latest`。如果当前 `s` 含有尚未落盘的 pending user，先保留该 pending 状态；只有当 `Session.load()` 的磁盘对象确认是同一 turn 或包含更完整 suffix 时才替换。替换决策必须是 append-only：磁盘缺少 sidecar 已有的 terminal row 时，不能用磁盘对象覆盖内存对象。

锁内刷新后重新执行：

1. `prepare_cron_session_for_reply(s)`；
2. `_next_turn_key(s.messages)`；
3. `_resolve_compatible_session_model_state(...)`（仅当刷新改变了 profile/model/provider 依据）；
4. `_prepare_chat_start_session_for_stream(...)`。

这样 pending save、turn journal 的 submitted event 和 worker 启动参数来自同一个 Session 快照。

### 6. Cron materialization 与 cache 保持一致

`materialize_cron_session()`、`_materialize_and_settle_cron_session_found()` 和 `prepare_cron_session_for_reply()` 可能通过 `Session.load()` 创建 sibling object。任何会保存该对象的路径必须：

- 持有相同 per-session lock；
- 从锁内最新 sidecar 开始；
- 只修改 Cron execution prefix、workspace/model 元数据或 Manifest settlement 所有的字段；
- 原样保留 `cron_execution_ended_at` 之后的 WebUI suffix，包括 terminal error；
- 保存成功后用该最新对象更新 `SESSIONS[sid]`，或明确失效 cache 让下一次 `get_session()` 强制重载。

不能仅比较消息数量。相同长度但内容不同的 sibling snapshot 也可能是陈旧对象。缓存新鲜度至少需要完整 sidecar stat identity 或 settled tail identity；具体采用哪种机制由第 4 节的失败测试确定。

在 `integration/crons/session_bridge.py:_materialize_and_settle_cron_session_found()` 中，`Session.load(materialized_sid)` 成功并完成 settlement 后，显式更新进程内缓存：

```python
from api.config import LOCK, SESSIONS

with LOCK:
    SESSIONS[materialized_sid] = session
    SESSIONS.move_to_end(materialized_sid)
```

该更新必须发生在最终 `session.save()` 成功之后，并且仍在 `_get_session_agent_lock(sid)` 内。若 settlement 不需要保存，也要把 full session 作为 canonical cache 对象；若 `Session.load()` 失败，不能用 metadata-only 对象回填 `SESSIONS`。对已有 sidecar 分支，`full.save()` 后同样执行缓存替换，避免下一次 `get_session()` 继续返回旧 sibling。

`api/routes.py` 当前 GET session 的 materialization 分支不要再单独调用一个锁外 reconciliation/save；所有 Cron prefix 更新统一经过上述 session lock 和缓存替换路径。这样 GET、Cron history、chat-start 三个入口不会各自持有不同的 Session writer。

### 7. 保留现有错误消息结构并增加可观测性

本次不扩展 `append_persisted_provider_error_message()` 的持久化字段。错误行仍通过它在 transcript 中紧跟所属 user turn；turn/stream/run 的执行关联继续由既有 turn journal 和 run journal 表达。

新增 `_turn_key` 或 `_stream_id` 不能阻止 sibling Session 旧快照覆盖 sidecar，也不会被现有 message merge identity 自动用于冲突消解。因此这类 schema 扩展不作为本次修复内容。

当前 provider error 路径已经先追加 sidecar error、调用 `Session.save()`，再通过 `put('apperror', ...)` 追加 run journal 并发布 SSE。保留该顺序，不新增 `PersistedErrorResult` 或额外存储格式。

需要修改的是当前静默异常处理：

```python
try:
    s.save()
except Exception:
    pass
```

改为结构化错误日志，至少包含：

- `session_id`
- `stream_id`
- `turn_key`
- `stage=provider_error_sidecar_save`
- exception type

sidecar 保存失败后仍调用 `put('apperror', ...)`，让 run journal 和在线用户尽可能获得终止事件。`put()` 已分别记录 journal failure 与 queue failure；本 RFC 不在同一文件系统上增加无边界重试。仅沿用 `Session.save()` / `_safe_replace()` 已有的原子写和平台重试语义。

不增加任何 `state.db` 写入。恢复时首先以 sidecar 的完整 transcript 为准；run journal 仅用于现有 stream 重连、审计和明确触发的恢复流程，普通 session GET 不在每次读取时重放 journal。

具体处理 `api/streaming.py` 两个 provider-error 分支（约 9717 和 10877）：保留 `_append_persisted_provider_error_message()`、`s.last_error_at = time.time()`、`s.save()`、journal/SSE 的顺序，只替换静默异常：

```python
try:
    s.save()
except Exception as exc:
    logger.warning(
        'provider error sidecar save failed session_id=%s stream_id=%s '
        'turn_key=%s stage=provider_error_sidecar_save error_type=%s',
        s.session_id,
        stream_id,
        _manifest_turn_key,
        type(exc).__name__,
    )
```

错误消息不增加 `_turn_key` 或 `_stream_id`。`_manifest_turn_key` 只用于日志、artifact settlement 和现有 turn journal payload；`stream_id` 只用于 run journal、日志和当前流状态。

## 状态空间

| 场景 | 预期模型行为 | 预期 transcript 行为 |
| --- | --- | --- |
| 新建 no-agent Cron session，Profile 有完整默认绑定 | sidecar 保存 Profile 默认 model/provider；不写 `unknown` | 物化 execution prefix；execution 审计不从 Session 模型推断 |
| 新建 no-agent Cron session，Profile 默认绑定不完整 | 保存空 model/provider；运行前 fail closed | 物化 execution prefix，不选择任意模型 |
| Cron session 有有效 model/provider | 原样使用并由现有校验确认 | 新 turn 追加，不改变旧 suffix |
| 已绑定的 no-agent Cron session 首次 follow-up | 使用 sidecar 已保存绑定，即使 Profile 默认后来变化 | 新 turn 追加，不改变 execution prefix |
| Cron session model=`unknown`，profile 有完整默认 | 使用 profile 默认；chat-start 保存实际绑定 | 保留旧 transcript，分配下一个 turn key |
| model=`unknown`，profile 默认缺失 | network 前 fail closed | 不创建 pending/live stream；旧 transcript 不变 |
| imported 非 Cron session 使用相同占位值 | 使用同一共享 resolver 修复 | 与普通 chat-start 契约一致 |
| 第一次 provider 失败后再次发送相同文本 | 第二次重新使用已解析绑定 | `user A/error A/user B/error B` 全部保留 |
| Cron reconciliation 发生在两次 follow-up 之间 | 不改变 follow-up 模型绑定 | 只更新 execution prefix，原样保留 suffix |
| sibling Session 对象保存 | 锁内先刷新 canonical sidecar | 不能缩短或覆盖已落盘 terminal row |
| sidecar save 失败 | 仍尝试 journal/SSE；记录结构化错误 | 不宣称 sidecar 持久化成功；不以写入 `state.db` 作为兜底 |
| 用户取消 | 不生成 provider error | 按既有取消契约记录，不与 error 去重混用 |

## 代码改动地图

| 优先级 | 文件/接缝 | 改动 |
| --- | --- | --- |
| P0 | `integration/crons/execution_model.py:read_profile_default_binding()` | 新增只读 Profile model/provider helper；只读 `config.yaml`/环境，不做 live catalog fallback |
| P0 | `integration/crons/session_bridge.py:_materialize_cron_session_found()` | 计算 `session_model/session_provider`；no-agent 无 execution model 时绑定 target Profile 默认；已有绑定不覆盖 |
| P0 | `api/models.py:import_cli_session()` | 在参数末尾增加 `model_provider=None`，并传给 `Session(...)`，保持位置参数兼容 |
| P0 | `api/routes.py:_resolve_compatible_session_model_state()` | 新增 `_is_unresolved_session_model()`；在 model/provider fast path 前处理历史 `unknown` |
| P0 | `api/routes.py:_validate_start_model()` | 新增统一运行前 gate；拒绝空/占位 model，返回 `session_model_unresolved` |
| P0 | `api/routes.py:_start_run()` | adapter 选择前调用运行前 gate，避免适配器路径绕过校验 |
| P0 | `api/routes.py:_start_chat_stream_for_session()` | session lock 内调用 `_reload_session_for_locked_start()`；刷新后重算 Cron prefix、model/provider 和 next turn key |
| P0 | `integration/crons/session_bridge.py:_materialize_and_settle_cron_session_found()` | 最终 save 后用 full Session 替换 `SESSIONS[sid]`；禁止 metadata-only 对象进入缓存 |
| P0 | `api/routes.py:handle_get()` Cron materialization 分支 | 删除锁外重复 reconciliation/save，统一复用 session_bridge 的锁内路径 |
| P0 | `api/streaming.py` 两个 provider-error 分支 | 保持错误消息 schema；将 `except: pass` 改为带 session/stream/turn/stage 的结构化日志 |
| P0 | `tests/test_cron_materialized_model.py`、`integration/tests/crons/test_session_bridge.py` | 新会话绑定、历史 unknown 回退、缓存替换和 prefix/suffix 保留测试 |
| P0 | `tests/test_issue1855_resolve_model_provider_fast_path.py`、新增 start-gate 测试 | resolver fast path、显式 unknown、无默认模型时 fail closed |
| P0 | `integration/tests/test_chat_provider_errors.py`、新增 shrink 回归测试 | 两次相同文本、两个 turn 的错误保留；save 失败仍发 journal/SSE |
| P1 | `api/models.py` cache freshness | 仅在失败测试证明共享 cache freshness 是 shrink 原因时，补充 same-count/sibling freshness 判定 |

本修复不要求修改 `/Users/wzq/Downloads/NLP-PyProject/hermes-agent`。Agent 侧只读核对 Provider retry 和 transcript ownership；若后续发现 Agent 也把占位模型作为有效配置接受，另开独立、单一逻辑变更。

本修复不包含 `state.db` migration、schema 变更或新的数据库写路径。

## 测试计划

测试必须先在当前实现上失败，再应用修复。

### 模型 resolver

1. 在 `tests/test_cron_materialized_model.py` 增加 `test_no_agent_materialize_binds_profile_default_model_provider`：mock `read_profile_default_binding()` 返回固定 model/provider，调用 `_materialize_cron_session_found()`，reload sidecar 断言两个字段均正确且 `model != "unknown"`。
2. 增加 `test_no_agent_materialize_does_not_use_live_catalog`：让 `get_available_models()` 抛错，物化仍能使用 config.yaml 默认；证明新建路径没有网络 catalog 依赖。
3. 增加 `test_materialized_binding_survives_profile_default_change`：物化后修改 Profile 配置，再调用 chat-start 解析，断言仍使用 sidecar 已保存绑定，除非请求显式换模型。
4. 增加 `test_no_agent_materialize_missing_profile_binding_fails_closed`：Profile 默认不完整时 sidecar model/provider 为空；调用启动入口断言 `type=session_model_unresolved`，`active_stream_id`、SSE channel、worker 均未创建。
5. 保留/扩展 `test_cron_materialize_preserves_state_db_model`：Agent-backed execution 有真实 model 时不替换成 Profile 默认。
6. 在 `tests/test_issue1855_resolve_model_provider_fast_path.py` 增加 `model="unknown" + provider="custom" + profile default="glm-5.3-flash"`：resolver 返回 Profile 默认，不进入原 fast path。
7. 增加显式 `explicit_model_pick=True, model="unknown"`：resolver 返回 unresolved，启动 gate 拒绝，不能静默替换默认模型。
8. imported 非 Cron session 使用 `unknown`：得到相同历史兼容修复；有效 custom model/provider 的现有 fast path 测试必须继续通过。

### 两次失败的 transcript 保留

构造一个带 execution boundary 的 Cron sidecar，并执行：

1. 第一次发送“文件呢”，绑定 `turn:2/stream:A`；mock Provider 返回 terminal error A。
2. 断言从磁盘 reload 后 error A 存在，run journal A 为 `errored`。
3. 在第二次发送前触发实际 Cron materialize/reconcile 和 session GET/cache 路径。
4. 第二次发送同一文本，必须绑定 `turn:3/stream:B`；mock Provider 返回 terminal error B。
5. reload sidecar，断言精确顺序为 `turn:1 prefix -> turn:2 user/error A -> turn:3 user/error B`。
6. 断言 `.json.bak` 不因第二次 chat-start 的 transcript shrink 新建或更新。
7. 断言 Agent `state.db` 有两个用户行，但没有 WebUI 合成 assistant error 行。
8. 断言测试前后 `state.db` schema 不变，且未新增 per-turn outcome/error 表。

对应测试建议放在 `integration/tests/crons/test_session_bridge.py` 或新增 `tests/test_cron_followup_error_persistence.py`，不要只测试 `_merge_display_messages_after_agent_result()`。测试必须使用真实 `Session.save()`、`SESSIONS` cache、`_get_session_agent_lock()`、Cron materialization 和 `get_session()` 接缝。

### sibling/cache 与错误路径

- 一个 sibling `Session.load()` 对象在 error A 保存前创建，error A 保存后再尝试启动下一 turn；断言 `_reload_session_for_locked_start()` 返回磁盘最新 transcript，并更新 `SESSIONS[sid]`。
- sidecar 与 cache 消息数相同但 tail identity 不同；断言旧对象不能覆盖新对象，必要时通过 `_cached_session_lags_disk()` 的 stat/tail 检查触发 reload。
- `Session.save()` 在 provider error 阶段抛出；断言日志包含 `session_id/stream_id/turn_key/stage=provider_error_sidecar_save`，且 `put('apperror')` 仍被调用。
- `api/routes.py:handle_get()`、Cron materialization 和 chat-start 交错执行；断言最终 sidecar 的 message count 单调不减，且无 metadata-only stub 被写入。
- 两个不同 user turn 即使错误文案和类型相同，也按 `user A/error A/user B/error B` 的顺序分别保留。
- Cron execution prefix 更新时，两个 follow-up suffix 完全不变。

### 验证命令

遵守仓库测试入口：

```bash
./scripts/test.sh tests/test_cron_materialized_model.py
./scripts/test.sh integration/tests/crons/
./scripts/test.sh tests/test_issue1855_resolve_model_provider_fast_path.py
```

新增测试文件按实际共享接缝命名。端到端验证使用隔离的 `HERMES_HOME`、`HERMES_WEBUI_STATE_DIR` 与本地 stub Provider，不使用真实 API key。

## 实施顺序

1. 保存真实 sidecar、`.bak`、两个 run journal 和两份 request dump 的最小脱敏 fixture。
2. 先写 `test_no_agent_materialize_binds_profile_default_model_provider`，在当前代码上确认它因写入 `unknown` 失败。
3. 写“两次相同提交、第一次错误消失”的失败测试，确认复现的是原问题。
4. 写共享 resolver 的历史 `unknown` 失败测试和 start-gate 测试。
5. 实现 `read_profile_default_binding()`，并修改 `import_cli_session()` 的末尾可选参数和 Cron 物化调用。
6. 修复 resolver 占位值分支和 `_validate_start_model()` 运行前 gate。
7. 用临时诊断钉住 transcript shrink writer；确认后删除诊断代码。
8. 实现 `_reload_session_for_locked_start()`、Cron materialization cache 替换和 GET 锁外保存移除。
9. 将两个 provider-error 分支的静默 `Session.save()` 异常改为结构化日志，不扩展错误消息 schema。
10. 运行受影响测试及相邻 resolver、Cron materialization、session recovery 测试。
11. 使用隔离状态执行两轮真实 HTTP follow-up，reload 后核对 sidecar 顺序。

模型修复和 transcript 修复建议作为两个逻辑 commit：

- `修复占位模型进入 Provider 请求的问题`
- `防止后续会话快照覆盖已保存错误`

## 发布与回滚

1. 先发布 Cron 物化源头修复、共享 resolver 和 network 前 gate，既阻止新 sidecar 产生 `unknown`，也阻止任何占位模型进入 Provider 请求。
2. 再发布 Session/error persistence 修复；部署后观察 transcript shrink backup、sidecar save failure 和 turn key conflict 指标。
3. 对历史 `model=unknown` session 不做批量猜测或改写；首次 follow-up 根据所属 profile 默认模型解析并保存实际绑定。
4. 对已有 `.json.bak` 中多出的 terminal error，使用现有 session recovery/audit 机制单独评估；旧数据不按错误文案自动批量合并。

## 验收标准

- 复现会话不会再向 Provider 发送 `model=unknown`。
- 后续新建的 no-agent Cron sidecar 不再包含字符串 `model="unknown"`。
- no-agent 新 sidecar 在物化完成时已保存 target Profile 的完整默认 model/provider；Profile 后续变更不静默改写已有会话绑定。
- Cron execution 是否实际使用 LLM 仍以 Agent/Cron execution record 为准，不从 `Session.model` 推断。
- profile 有默认模型时使用并保存该完整绑定；没有默认模型时不创建 worker、不发网络请求。
- 两次相同“文件呢”分别保留为 `turn:2` 和 `turn:3`。
- 两次 provider error 按各自 user turn 后的 transcript 顺序存在；reload、reconcile 和下一次 chat-start 后都不丢失。
- `append_persisted_provider_error_message()` 的持久化 schema 不增加 `_turn_key` 或 `_stream_id`。
- 后续 Session 保存不再生成“旧 sidecar 消息更多”的 shrink backup。
- Agent `state.db` 不出现 WebUI 合成的 assistant error。
- 不新增数据库表，不修改 `state.db` schema，也不复用 `sessions.end_reason` 表示单轮错误。
- sidecar 保存失败可从结构化日志定位，journal/SSE 仍按现有 best-effort 语义尝试。
- 普通有效 model/provider fast path 的行为与性能不退化。

## 待实现阶段确认

1. 失败测试最终钉住的 shrink writer 是 chat-start stale cache、Cron sibling save，还是两者共同作用；只修改测试证明的共享接缝。
2. cache freshness 使用 sidecar stat identity 还是 settled tail identity；优先复用现有机制，避免每次 cache hit 全量解析 sidecar。
3. 旧 journal 仅用于人工 recovery audit；默认不凭相同错误文案自动写回 sidecar。

## 本次实施记录

已完成以下代码切片：

- `integration/crons/execution_model.py`：新增 `read_profile_default_binding()`，仅读取 Profile 配置/环境，不触发 catalog discovery。
- `integration/crons/session_bridge.py`：no-agent 物化不再制造 `unknown`；补齐 Profile model/provider；物化保存后同步 `SESSIONS` 缓存。
- `api/models.py`：`import_cli_session()` 末尾增加兼容的 `model_provider=None` 参数并传给 `Session`。
- `api/routes.py`：resolver 在 fast path 前规范化历史占位值；新增 `_validate_start_model()`，在 legacy streaming 与 runtime adapter 入口启动前 fail closed；Cron 启动锁内重载完整 sidecar。
- `api/streaming.py`：provider error sidecar 保存失败改为带 session、stream、turn、stage 的结构化 warning。
- 回归测试覆盖 no-agent 绑定、历史 `unknown` resolver 和 Profile binding。

已执行并通过：

```bash
./scripts/test.sh integration/tests/crons/ tests/test_issue1855_resolve_model_provider_fast_path.py
python3 -m py_compile api/models.py api/routes.py api/streaming.py \
  integration/crons/execution_model.py integration/crons/session_bridge.py
```

完整的双 follow-up provider-error 端到端测试仍应在后续切片中补齐，以覆盖真实 stream A/B、materialize/reconcile 与 sidecar shrink writer 的交错时序。
