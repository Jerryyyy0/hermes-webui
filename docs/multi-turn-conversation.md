# 多轮会话交互流程

本文档说明 Hermes WebUI 在**同一个会话内进行多轮对话**时，前端与后端之间的完整交互逻辑、数据流及关键实现细节。

---

## 1. 架构总览

多轮对话基于 **同一 `session_id` + 每轮独立的 `stream_id`** 组织。每一轮对话遵循严格的生命周期：

```
用户发送  →  POST /api/chat/start  →  后台 Agent 启动  →  SSE 推送事件  →  'done' 落盘
```

### 涉及的主要文件

| 文件 | 作用 |
|------|------|
| `api/routes.py` | HTTP 路由分发：POST `/api/chat/start`、GET `/api/chat/stream` 等 |
| `api/streaming.py` | Agent 流式执行：`_run_agent_streaming()`、`_handle_chat_steer()` |
| `static/messages.js` | 前端发送消息、SSE 连接与事件处理 |
| `static/ui.js` | 前端 UI 辅助函数 |
| `api/session_manifest.py` | 会话 Manifest（成果/待办/参考）管理 |
| `api/compression_anchor.py` | 自动压缩锚点管理 |

---

## 2. 核心数据模型

### Session（会话）

每个 session 在服务端持久化存储，关键字段：

```
s = {
  session_id: str,           # 会话唯一 ID，跨轮不变
  messages: Message[],       # 展示用消息列表，每轮累加
  context_messages: Message[],  # 发给模型的完整上下文（含压缩历史）
  active_stream_id: str|None,  # 当前活跃的 stream ID
  pending_user_message: str|None,  # 尚未被 Agent 消费的用户消息
  pending_attachments: [],    # 待处理的附件
  workspace: str,             # 工作区路径
  model: str,                 # 模型名称
  model_provider: str,        # 模型提供商
  profile: str,               # 所属 profile
  tool_calls: ToolCall[],     # 全会话工具调用记录
  ...
}
```

### Stream（流）

每轮对话生成一个独立 stream：

```python
stream_id = uuid.uuid4().hex  # 每轮新生成
STREAMS[stream_id] = StreamChannel  # SSE 事件队列
```

### 消息列表的累积

```
# 第一轮完成后
s.messages = [
  {role: "user",     content: "第一轮的问题"},
  {role: "assistant", content: "第一轮的回答", tool_calls: [...]},
]

# 第二轮完成后
s.messages = [
  {role: "user",     content: "第一轮的问题"},
  {role: "assistant", content: "第一轮的回答", tool_calls: [...]},
  {role: "user",     content: "第二轮的问题"},
  {role: "assistant", content: "第二轮的回答", tool_calls: [...]},
]

# 以此类推...每轮追加一对 user + assistant 消息
```

---

## 3. 完整交互流程

### 3.1 前端发送消息（`messages.js`）

```
用户按下回车/发送按钮
  │
  ├─ 如果当前有活跃 stream（S.activeStreamId !== null）
  │   ├─ steer 模式  → POST /api/chat/steer（注入提示，不中断）
  │   ├─ interrupt 模式 → 队列消息 + 取消当前流
  │   └─ queue 模式 → 队列消息，等 drain 自动发送
  │
  └─ 如果没有活跃 stream（正常发送路径）
      │
      ├─ 乐观 UI：push user msg → S.messages → renderMessages()
      ├─ 显示 thinking spinner
      ├─ POST /api/chat/start {session_id, message, model, workspace, ...}
      │
      ├─ 成功 → 获取 stream_id
      │   ├─ S.activeStreamId = stream_id
      │   ├─ 连接 SSE EventSource → /api/chat/stream?stream_id=xxx
      │   └─ 等待 SSE 事件
      │
      └─ 失败
          ├─ 409（session 已有活跃 stream）→ 队列消息
          ├─ 404（session 已被删除）→ 重置 UI
          └─ 其他错误 → 显示错误提示
```

### 3.2 后端处理 `/api/chat/start`（`routes.py` `_handle_chat_start`）

```
POST /api/chat/start
  Body: {session_id, message, model, workspace, model_provider, attachments, ...}
  │
  ├─ 1. 验证字段（session_id、message 必填）
  ├─ 2. 加载 session（get_session）
  ├─ 3. 解析 workspace、model、provider
  │     └─ 支持从 profile 配置中读取默认 provider/model
  │
  ├─ 4. 检查并发保护
  │     ├─ _active_stream_blocks_chat_start()
  │     │   → 检查 STREAMS 字典 + ACTIVE_RUNS 字典
  │     │   → 检查 pending_user_message 是否在宽限期内（30s）
  │     └─ 如果冲突 → 返回 409 {"error": "session already has an active stream"}
  │
  ├─ 5. _start_chat_stream_for_session()
  │     ├─ _prepare_chat_start_session_for_stream()
  │     │   ├─ s.workspace = workspace
  │     │   ├─ s.model = model
  │     │   ├─ s.active_stream_id = stream_id（新生成 uuid4）
  │     │   ├─ s.pending_user_message = msg
  │     │   ├─ s.pending_attachments = attachments
  │     │   ├─ 设置 provisional title（从消息内容推断）
  │     │   ├─ 如果 save_mode == "eager" → 立即写入用户消息到 s.messages
  │     │   └─ s.save()
  │     │
  │     ├─ 注册 SSE channel（create_stream_channel → STREAMS[stream_id]）
  │     │
  │     └─ 启动后台线程 _run_agent_streaming(session_id, msg, model, ...)
  │
  └─ 6. 返回 JSON {stream_id, effective_model, ...}
```

### 3.3 Agent 流式执行（`streaming.py` `_run_agent_streaming`）

```
_run_agent_streaming(session_id, msg_text, model, workspace, stream_id, ...)
  │
  ├─ 1. 注册 ACTIVE_RUNS
  ├─ 2. 初始化 RunJournal（崩溃恢复用）
  ├─ 3. 创建 cancel_event 信号量
  ├─ 4. 加载 session，获取 _previous_messages（全部历史）
  │     └─ _previous_messages = s.messages（含之前所有轮次）
  │
  ├─ 5. 构建 Agent（含预设、技能、MCP 等）
  │     └─ agent.run_conversation(msg_text, _previous_messages, ...)
  │         └─ Agent 以全部历史为上下文开始执行
  │
  ├─ 6. Agent 执行过程中实时推送 SSE 事件
  │     ├─ 'token' {text}               → 流式文本
  │     ├─ 'reasoning' {text}           → 推理过程
  │     ├─ 'interim_assistant' {text}   → 中间小结（工具调用后）
  │     ├─ 'tool' {name, args, tid}     → 工具开始
  │     ├─ 'tool_complete' {name, result, tid} → 工具完成
  │     ├─ 'manifest_delta' {...}       → 侧栏增量更新
  │     ├─ 'todo_state' {todos, ts}     → Todos 面板快照
  │     ├─ 'browser_preview' {url}      → 浏览器预览
  │     ├─ 'approval' {...}             → 需要用户批准
  │     ├─ 'clarify' {...}              → 需要用户澄清
  │     ├─ 'state_saved' {...}          → 持久化状态变更
  │     ├─ 'title' {title}              → 标题更新
  │     ├─ 'context_status' {...}       → 上下文加载状态
  │     ├─ 'goal' {...}                 → 目标评估状态
  │     └─ 'goal_continue' {...}        → 目标继续（自动下一轮）
  │
  ├─ 7. Agent 完成（run_conversation 返回）
  │     ├─ 检查 cancel 信号 → 走取消路径
  │     ├─ 合并/去重 _result_messages
  │     ├─ 清理 XML 工具调用标签（DeepSeek 等）
  │     ├─ 处理自动压缩（session_id 可能被旋转）
  │     ├─ 持久化：
  │     │   ├─ s.context_messages = _next_context_messages（压缩后）
  │     │   ├─ s.messages = _merge_display_messages(...)（展示用）
  │     │   ├─ s.active_stream_id = None（清除）
  │     │   └─ s.save()
  │     │
  │     ├─ 发送 'done' 事件 {session: {...}, usage: {...}}
  │     ├─ 发送 'metering' 事件（最终 TPS 指标）
  │     │
  │     └─ 发送 'stream_end' 事件 → SSE 连接关闭
  │
  └─ 8. finally 块
        ├─ 停止 metering ticker
        ├─ 注销 approval/clarify 回调
        ├─ 清理环境变量
        ├─ 清理 STREAMS/CANCEL_FLAGS/AGENT_INSTANCES
        ├─ 持久化 inflight 状态（用于恢复）
        └─ 发布 session_list_changed 事件
```

### 3.4 前端 SSE 事件处理（`messages.js` `attachStream`）

```
EventSource → /api/chat/stream?stream_id=xxx
  │
  ├─ 'token' 事件
  │   ├─ assistantText += d.text
  │   ├─ syncInflightAssistantMessage()
  │   └─ _scheduleRender() → rAF 节流更新 DOM
  │
  ├─ 'reasoning' 事件
  │   ├─ reasoningText += d.text
  │   ├─ liveReasoningText += d.text
  │   └─ _updateLiveThinkingCard()
  │
  ├─ 'tool' 事件（工具开始）
  │   ├─ upsertLiveToolCall(d, 'start')
  │   ├─ appendLiveToolCard()
  │   └─ _freshSegment = true（准备下一段助理文本）
  │
  ├─ 'tool_complete' 事件
  │   ├─ upsertLiveToolCall(d, 'complete')
  │   ├─ noteWorkspaceMutationsFromToolCall()
  │   └─ refreshOpenPreviewIfMutated()
  │
  ├─ 'manifest_delta' 事件
  │   ├─ HermesSessionInspector.applyDelta()
  │   └─ HermesIntegrationWorkspace.onManifestDelta()
  │
  ├─ 'todo_state' 事件
  │   ├─ 检查时间戳防过期
  │   ├─ S.todos = d.todos（整体替换）
  │   └─ scheduleTodosRefresh()
  │
  ├─ 'approval' 事件 → showApprovalForSession()
  ├─ 'clarify' 事件 → showClarifyForSession()
  │
  ├─ 'done' 事件（核心终结事件）
  │   ├─ _streamFinalized = true（防止后续 rAF 干扰）
  │   ├─ _cancelAnimationFramePendingStreamRender()
  │   ├─ finalizeThinkingCard()
  │   │
  │   ├─ 更新会话状态
  │   │   ├─ S.session = d.session（服务端确认的最新状态）
  │   │   ├─ S.messages = d.session.messages
  │   │   ├─ S.activeStreamId = null ★★★ 关键：标记本轮完成
  │   │   ├─ S.toolCalls = _mergeSettledToolCallsWithLiveMetadata()
  │   │   └─ S.lastUsage = d.usage
  │   │
  │   ├─ 计算本轮 token 用量 delta（用于 per-turn 显示）
  │   ├─ 持久化 reasoning 到最后一条 assistant 消息
  │   ├─ 清理运算中状态
  │   │   ├─ clearLiveToolCards()
  │   │   ├─ S.busy = false
  │   │   ├─ clearOwnerInflightState()
  │   │   └─ removeThinking()
  │   │
  │   ├─ 刷新 Manifest（成果/待办/参考）
  │   ├─ 渲染消息（renderMessages）
  │   └─ 标记会话已读
  │
  ├─ 'stream_end' 事件
  │   └─ SSE 连接关闭
  │
  ├─ 'cancel' 事件
  │   └─ 显示取消信息，清理状态
  │
  └─ 'apperror' 事件
      └─ 显示错误提示
```

### 3.5 第二轮及后续轮次

```
第二轮交互
  │
  ├─ 第一轮完成后：S.activeStreamId === null
  │   → 输入框可用，用户可输入新消息
  │
  ├─ 重复 3.1-3.4 的完整流程
  │   └─ session_id 不变（同一会话）
  │
  ├─ 关键区别：
  │   ├─ 后端加载 session 时，s.messages 包含第一轮全部消息
  │   ├─ Agent 以完整历史 [user, assistant, user, ...] 为上下文
  │   ├─ 新的 stream_id 生成（旧的已清理）
  │   └─ 新的 SSE 连接建立（旧的已关闭）
  │
  └─ 可以持续 N 轮，直到会话关闭或自动压缩
```

---

## 4. 并发控制与防重复

### 4.1 前端侧

- `S.activeStreamId`：非空时阻止新消息发送（走队列/steer/中断）
- `LIVE_STREAMS[activeSid]`：记录当前活跃的 SSE 连接，防止重复连接

### 4.2 后端侧

`_active_stream_blocks_chat_start()`（`routes.py` `~11883`）：

```python
def _active_stream_blocks_chat_start(session, stream_id):
    # 检查 STREAMS 字典（活跃的 SSE 通道）
    if stream_id in STREAMS:
        return True

    # 检查 ACTIVE_RUNS（后台 worker 仍存活）
    if stream_id in ACTIVE_RUNS:
        return True

    # 检查 pending_user_message 宽限期（默认 30s）
    if session.pending_user_message and age < grace_seconds:
        return True

    return False
```

---

## 5. `/api/chat/steer` — 不中断的指导注入

当 Agent 正在运行时，用户可以通过 steer 模式发送指导文本。

`_handle_chat_steer()`（`streaming.py` `~7737`）：

```
POST /api/chat/steer {session_id, text}
  │
  ├─ 1. 从 SESSION_AGENT_CACHE 获取缓存的 Agent 实例
  ├─ 2. 验证 Agent 支持 steer() 方法
  ├─ 3. 验证 session 有活跃的 active_stream_id
  ├─ 4. 验证 stream 仍在 STREAMS 中
  ├─ 5. 调用 agent.steer(text)
  │     └─ 将文本注入到下一个工具结果的消息中
  │     └─ Agent 在下一轮迭代中看到指导内容
  └─ 6. 返回 {accepted: true/false, fallback: reason/null}
```

Steer 模式的 fallback 路径：

| fallback 原因 | 处理方式 |
|---------------|----------|
| `no_cached_agent` | 降级为中断 + 队列 |
| `agent_lacks_steer` | 降级为中断 + 队列 |
| `session_not_found` | 返回错误 |
| `not_running` | 正常发送新消息 |
| `stream_dead` | 返回错误 |
| `steer_error` | 返回错误 |

---

## 6. 自动压缩与 Session ID 旋转

在长会话中，当上下文超出阈值时会触发自动压缩。根据压缩引擎和上下文状态的不同，压缩可能**原地进行**（不改变 session_id），也可能导致 **session_id 旋转**（创建延续会话）。

---

### 6.1 压缩模式

压缩模式由 `Session` 上的 `compression_anchor_mode` 和 `compression_anchor_engine` 字段决定，前端通过 `_compressionModeForSession()`、`_compressionEngineForSession()` 读取（`static/ui.js:7436`）。

#### 6.1.1 轻量压缩（Context Compaction / `summary_compaction`）

| 项目 | 说明 |
|------|------|
| 模式值 | `summary_compaction`（默认） |
| UI 标签 | "上下文压缩"（Context compaction） |
| 引擎 | `compressor`（默认） |

行为：LLM 将早期对话总结为一段 `[CONTEXT COMPACTION — REFERENCE ONLY]` 标记，原始消息被替换掉。消息条数大幅减少、token 节省，但原始内容不可逆地丢失（保存在 `pre_compression_snapshot` 备份会话文件中）。

相关代码：
- 前端渲染区分：`_engineAwareCompressionCopy()`（`static/ui.js:7449`）在 `engine!=='lcm'` 且 `mode!=='lossless_retrieval'` 时显示 Context Compaction 标签
- 后端压缩标记检测：`is_context_compression_marker()`（`api/compression_anchor.py:56`）识别 `[context compaction`、`context compaction` 等开头的内容

#### 6.1.2 重度压缩（Indexed Context / `lossless_retrieval`）

| 项目 | 说明 |
|------|------|
| 模式值 | `lossless_retrieval` |
| UI 标签 | "已索引上下文"（Indexed context） |
| 引擎 | `lcm`（Lossless Context Management） |

行为：使用 LCM 引擎，将历史消息索引存储到外部，**不丢失内容**，后续轮次可通过上下文工具按需检索旧消息。更重的代价：需要额外存储和检索机制。

前端展示：
```javascript
// static/ui.js:7449
function _engineAwareCompressionCopy(engine, mode){
  if(engine==='lcm' || mode==='lossless_retrieval'){
    return {
      label: t('retrieval_context_label'),       // "已索引上下文"
      preview: t('retrieval_context_preview'),     // "较早消息已存储，可通过上下文工具检索"
    };
  }
  return {
    label: t('context_compaction_label'),         // "上下文压缩"
    preview: t('reference_only_label'),            // "仅供参考"
  };
}
```

---

### 6.2 新会话创建

新会话在以下场景创建：

| 场景 | 触发方式 | API / 路径 | 特征 |
|------|----------|------------|------|
| **用户点击"New Conversation"** | 侧栏按钮 / `/new` 命令 | `POST /api/session/new` → `api/routes.py:6961` | 全新会话，无 parent |
| **Fork / 分支** | `/branch` 命令 / "Fork from here" 按钮 | `POST /api/session/branch` → `api/routes.py:7681` | `session_source="fork"`，`parent_session_id` 指向源会话 |
| **切换 Profile** | 用户切换配置 | — | 自动为新 profile 创建会话 |
| **压缩延续** | Agent 内部压缩耗尽自动触发 | — | `session_id` 旋转，旧会话标记 `pre_compression_snapshot` |

Fork 与压缩延续的核心区别：

```
压缩延续 (Continuation)                     Fork / 分支
─────────────────                          ──────────
自动发生（agent 内部决策）                   用户主动触发
旧会话标记 pre_compression_snapshot          普通会话
侧栏中折叠为一条                              显示为独立会话
end_reason=compression                       session_source=fork
通过 parent_session_id 遍历拼接消息           不拼接，完全独立
```

判断逻辑：`_is_continuation_session()`（`api/agent_sessions.py:214`）：
```python
def _is_continuation_session(parent, child):
    if child.get('session_source') == 'fork':
        return False  # Fork 不是延续
    if parent.get('end_reason') not in {'compression', 'cli_close'}:
        return False  # 只有 compression/cli_close 才是延续
    # 父子 source 必须一致，时间线必须连续
    ...
```

---

### 6.3 会话轮次不分裂的场景

以下场景中，`session_id` **保持不变**，所有轮次在同一个会话内累积：

#### 场景 A：正常多轮对话

- 每次 user message → assistant reply 都在同一会话中追加
- `s.messages` 数组逐轮增长：`[user1, asst1, user2, asst2, ...]`
- 不触发任何压缩行为

#### 场景 B：手动压缩（`/compress`）

- 用户手动执行 `/compress` 或 `/compact`
- 后端 `_handle_session_compress()`（`api/routes.py:14128`）流程：

```
POST /api/session/compress {session_id, focus_topic?}
  │
  ├─ 构建 AIAgent，调用 context_compressor.compress(original_messages)
  ├─ 获取压缩后的消息列表（替换原始消息）
  │
  ├─ 加锁检查：stream 状态、消息内容是否在压缩期间被修改
  │
  ├─ 原地替换：
  │   ├─ s.messages = compressed
  │   ├─ s.context_messages = compressed
  │   ├─ s.tool_calls = []
  │   ├─ s.active_stream_id = None
  │   └─ s.save()
  │
  └─ session_id 不变，不创建新会话
```

- 压缩后旧消息不可逆丢失，但 `.json.bak` 备份文件可恢复

#### 场景 C：自动压缩（Agent 内部原地压缩）

- 在 `agent.run_conversation()` 内部，当上下文接近阈值但仍可压缩
- Agent 调用 `context_compressor.compress()` 对模型上下文做摘要
- `session_id` **不变**
- 压缩标记 `[CONTEXT COMPACTION — REFERENCE ONLY]` 出现在消息列表中
- `streaming.py:6730-6805` 检测压缩完成，设置锚点信息：

```python
# streaming.py:6740
visible_after = visible_messages_for_anchor(s.messages, auto_compression=True)
# 找到最后一个 [CONTEXT COMPACTION] 标记位置
for _mi, _m in enumerate(s.messages):
    if _is_context_compression_marker(_m):
        _last_marker_raw_idx = _mi
# 设置压缩锚点
s.compression_anchor_visible_idx = ...
s.compression_anchor_message_key = _compression_anchor_message_key(anchor_msg)
s.compression_anchor_summary = ...
```

---

### 6.4 会话轮次分裂的场景（Continuation / 延续会话）

当 Agent 内部的 context_compressor 判定上下文窗口**彻底耗尽**、**无法继续压缩**时，会决定创建**延续会话（continuation）**。这发生在 `run_conversation()` 内部（hermes-agent 库），WebUI 在 `streaming.py:6407` 通过检查 agent 的 `session_id` 是否变化来检测：

```python
# streaming.py:6407
_agent_sid = getattr(agent, 'session_id', None)
if _agent_sid and _agent_sid != session_id:
    # ── Session 旋转了！──
    old_sid = session_id
    new_sid = _agent_sid
    _compression_continuation_session_id = new_sid
```

#### 触发条件

1. **多次压缩已达上限** — `compression_count` 超过阈值，出现 `compression_exhausted` 错误（`streaming.py:892`）
2. **上下文长度超限且无法再压缩** — `context length exceeded` + `cannot compress further`
3. **最大压缩尝试次数耗尽** — `max compression attempts reached`

错误提示（`streaming.py:922`）：
```python
_is_compression_exhausted = (
    'compression_exhausted' in _err_lower
    or ('context length exceeded' in _err_lower and 'cannot compress further' in _err_lower)
    or ('context compression' in _err_lower and 'max compression attempts' in _err_lower)
)
# → 返回 hint: "The conversation context is too large to compress safely."
```

#### 分裂后的处理逻辑

```
Agent 完成 run_conversation()
  │
  ├─ 检测到 agent.session_id != session_id
  │
  ├─ 1. 迁移 session_id
  │     s.session_id = new_sid
  │     └─ 保留 profile、workspace 等字段
  │
  ├─ 2. 保存旧会话快照
  │     _preserve_pre_compression_snapshot(s, old_sid)
  │     └─ 旧文件标记 pre_compression_snapshot = true
  │
  ├─ 3. 建立父子链接
  │     s.parent_session_id = old_sid
  │     └─ 形成可遍历的链：new → old → old.parent → ... → root
  │
  ├─ 4. 锁迁移
  │     SESSION_AGENT_LOCKS[new_sid] = _agent_lock
  │     SESSION_AGENT_LOCKS.pop(old_sid, None)
  │
  ├─ 5. 缓存迁移
  │     SESSION_AGENT_CACHE[new_sid] = SESSION_AGENT_CACHE.pop(old_sid)
  │
  ├─ 6. SESSIONS 字典迁移
  │     SESSIONS[new_sid] = s
  │     SESSIONS.pop(old_sid, None)
  │
  └─ 7. 前端通知
      put('compressed', {
        old_session_id: old_sid,
        new_session_id: new_sid,
        continuation_session_id: new_sid,
      })
```

#### 前端拼接显示

当浏览延续会话时，后端通过 `_webui_sidecar_lineage_messages_for_display()`（`api/routes.py:2975`）自动将祖先会话的消息拼接在一起：

```python
def _webui_sidecar_lineage_messages_for_display(session, *, max_hops=20):
    segments = []
    current = session
    for _ in range(max_hops):
        parent_id = getattr(current, "parent_session_id", "")
        if not parent_id:
            break
        parent = Session.load(parent_id)
        if not getattr(parent, "pre_compression_snapshot", False):
            break  # 只有 pre_compression_snapshot 才拼接
        segments.append(parent)
        current = parent
    # 反转后拼接：最老的祖先在最前面
    merged = []
    for segment in reversed(segments):
        merged = merge_session_messages_append_only(merged, segment.messages)
    return merge_session_messages_append_only(merged, session.messages)
```

侧栏折叠：`_project_agent_session_rows()`（`api/agent_sessions.py:271`）将延续链合并为**一条逻辑侧栏记录**。

#### 对比总结

| 场景 | 是否分裂 | session_id | 旧消息去向 | 触发方式 |
|------|----------|------------|------------|----------|
| 正常对话 | **否** | 不变 | 正常追加 | 用户发消息 |
| 手动 `/compress` | **否** | 不变 | 被压缩摘要替换 | 用户手动 |
| 自动压缩（阈值内） | **否** | 不变 | 被压缩摘要替换 | 自动 |
| 自动压缩（耗尽） | **是** | 旋转为新 ID | 保存为 `pre_compression_snapshot` | 自动 |
| 用户点 New | — | 全新 | 无 | 手动 |
| `/branch` fork | — | 全新 | 复制到新会话 | 手动 |

---

### 6.5 关键代码位置

| 功能 | 文件 | 行号（约） |
|------|------|-----------|
| 自动压缩 session_id 旋转检测 | `api/streaming.py` | `6407` |
| 压缩旋转的完整迁移逻辑 | `api/streaming.py` | `6407-6507` |
| 压缩完成后的锚点设置 | `api/streaming.py` | `6730-6805` |
| 压缩耗尽检测 | `api/streaming.py` | `892-927` |
| 手动压缩处理 | `api/routes.py` | `14128` `_handle_session_compress` |
| 压缩锚点工具函数 | `api/compression_anchor.py` | 全部 |
| 新旧会话拼接显示 | `api/routes.py` | `2975` `_webui_sidecar_lineage_messages_for_display` |
| 延续会话判断 | `api/agent_sessions.py` | `214` `_is_continuation_session` |
| 侧栏压缩链折叠 | `api/agent_sessions.py` | `271` `_project_agent_session_rows` |
| 创建新会话 | `api/routes.py` | `6961` `POST /api/session/new` |
| Fork 分支会话 | `api/routes.py` | `7681` `POST /api/session/branch` |
| 前端压缩模式读取 | `static/ui.js` | `7436` `_compressionModeForSession` |
| 前端压缩文案区分 | `static/ui.js` | `7449` `_engineAwareCompressionCopy` |
| 前端新会话创建 | `static/sessions.js` | `579` `newSession` |
| 前端分支命令 | `static/commands.js` | `1428` `cmdBranch` |
| `pre_compression_snapshot` 保存 | `api/streaming.py` | `2813` `_preserve_pre_compression_snapshot` |

---

## 7. 异常恢复路径

### 7.1 SSE 断线重连

```
SSE onerror
  │
  ├─ 检查 session 是否仍活跃
  ├─ GET /api/chat/stream/status?stream_id=xxx
  │
  ├─ 如果 stream 仍 active → 重新连接 SSE（含 run journal replay）
  ├─ 如果 stream 已结束但有 journal → replay 模式
  └─ 如果 stream 已结束无 journal → _restoreSettledSession()
      └─ 重新加载 session 的最新状态
```

### 7.2 页面关闭/隐藏

```javascript
// 页面隐藏时 SSE 断开 → 暂缓错误处理
// 页面重新可见时重连
document.addEventListener('visibilitychange', resume);
window.addEventListener('focus', resume);
window.addEventListener('pageshow', resume);
```

### 7.3 刷新恢复

```javascript
// localStorage 中保存 INFLIGHT 状态
const INFLIGHT_STATE_KEY = 'hermes-webui-inflight-state';

// 刷新后恢复：
// 1. 读取 INFLIGHT 状态（含 stream_id, messages）
// 2. GET /api/chat/stream/status 检查 stream
// 3. 如果 stream 仍活跃 → 重新连接 SSE + journal replay
// 4. 如果 stream 已结束 → 重新加载 session
```

---

## 8. 接口一览

| 接口 | 方法 | 用途 | 请求体/参数 | 返回 |
|------|------|------|-------------|------|
| `/api/chat/start` | POST | 发送消息，启动 Agent | `{session_id, message, model, workspace, ...}` | `{stream_id, effective_model, ...}` |
| `/api/chat/stream` | GET | SSE 流式连接 | `?stream_id=xxx` | SSE 事件流 |
| `/api/chat/stream/status` | GET | 检查 stream 状态 | `?stream_id=xxx` | `{active, replay_available}` |
| `/api/chat/cancel` | POST | 取消当前流 | `{stream_id}` | `{ok: true}` |
| `/api/chat/steer` | POST | 注入指导文本（不中断） | `{session_id, text}` | `{accepted, fallback}` |
| `/api/session/manifest` | GET | 拉取会话 Manifest | `?session_id=xxx` | `{artifacts, references, todos}` |

---

## 9. SSE 事件类型汇总

| 事件 | 阶段 | 触发时机 | 前端处理 |
|------|------|----------|----------|
| `token` | 流式 | 模型输出文本 token | 追加 assistantText → 渲染 |
| `reasoning` | 流式 | 模型推理/思考过程 | 更新 Thinking Card |
| `interim_assistant` | 流式 | 工具调用后的中间小结 | 插入 interim 气泡 |
| `tool` | 流式 | 工具开始执行 | 创建/更新 Live Tool Card |
| `tool_complete` | 流式 | 工具执行完成 | 标记完成，记录文件变更 |
| `manifest_delta` | 全阶段 | 成果/待办/参考增量 | 侧栏 Inspector 增量合并 |
| `todo_state` | 全阶段 | Todos 快照更新 | 整体替换 Todos 面板 |
| `approval` | 全阶段 | 需要用户批准工具调用 | 显示 Approval Card |
| `clarify` | 全阶段 | 需要用户澄清 | 显示 Clarify Card |
| `state_saved` | 全阶段 | 持久化状态变更 | Toast 提示 |
| `title` | 全阶段 | 会话标题更新 | 更新标题 |
| `context_status` | 全阶段 | 上下文预填充状态 | Composer 状态提示 |
| `goal` | 全阶段 | 目标评估状态 | Composer 状态 + Toast |
| `goal_continue` | 全阶段 | 目标驱动自动续轮 | 自动发起下一轮 |
| `browser_preview` | 流式 | 浏览器预览可用 | 打开预览/Toast |
| `metering` | 流式 | 周期性 TPS 更新 | 更新速率显示 |
| `done` | 终结 | Agent 完成本轮 | 替换消息、清空状态、启用输入 |
| `stream_end` | 终结 | SSE 流结束 | 关闭 EventSource |
| `cancel` | 终结 | 用户/系统取消 | 清理、显示取消信息 |
| `apperror` | 异常 | 应用级错误 | 显示错误提示 |

---

## 10. 关键代码位置

| 功能 | 文件 | 行号（约） |
|------|------|-----------|
| `/api/chat/start` 处理入口 | `api/routes.py` | `12305` `_handle_chat_start` |
| stream 启动与防重复 | `api/routes.py` | `11964` `_start_chat_stream_for_stream` |
| 持久化 pending 状态 | `api/routes.py` | `11831` `_prepare_chat_start_session_for_stream` |
| 活跃 stream 防重复检查 | `api/routes.py` | `11883` `_active_stream_blocks_chat_start` |
| Agent 后台执行主循环 | `api/streaming.py` | `4604` `_run_agent_streaming` |
| SSE `put` 函数（事件入队） | `api/streaming.py` | `4881` |
| `done` 事件发射 | `api/streaming.py` | `7425` |
| `/api/chat/steer` 处理 | `api/streaming.py` | `7737` `_handle_chat_steer` |
| SSE stream 连接处理 | `api/routes.py` | `9421` `_handle_sse_stream` |
| 前端发送消息入口 | `static/messages.js` | `~690` |
| 前端 `POST /api/chat/start` 调用 | `static/messages.js` | `946` |
| SSE 事件绑定（`_wireSSE`） | `static/messages.js` | `2466` |
| `done` 事件前端处理 | `static/messages.js` | `2836` |
| SSE 断线重连 | `static/messages.js` | `1532` `_reattachOrRestoreAfterDeferredStreamError` |
| INFLIGHT 状态持久化 | `static/ui.js` | `4854` |
