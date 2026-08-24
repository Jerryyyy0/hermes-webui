# Turn Key 后端逻辑说明

`turn_key` 是 Hermes WebUI 中标识"一轮用户-助手交互"的稳定标识符。本文档说明其在后端各层的生成、传递与消费方式，涉及 `/api/session`、`/api/session/manifest`、流式 SSE、前端渲染链路，以及**压缩场景下的对齐行为**（第 8 节）。

---

## 1. 什么是 turn_key

`turn_key` 的格式为 `turn:<N>`，其中 `N` 是一个单调递增的整数。

- 每轮交互由一条 `role=user` 消息发起。
- `turn_key` 以 **用户消息的 `_turn_key` 字段**为稳定锚点。
- 后端优先使用消息上持久化的 `_turn_key`；不存在时降级使用消息数组索引 `turn:<idx>`。

---

## 2. turn_key 的生成与持久化

### 2.1 生成下一个 turn_key：`_next_turn_key()`

**文件**: `integration/session_manifest/manifest.py` 第 905–917 行

```python
def _next_turn_key(messages: list) -> str:
    """通过扫描现存用户消息，返回下一个稳定的 turn key。"""
    max_num = 0
    for msg in messages or []:
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        key = msg.get('_turn_key', '')
        if key and key.startswith('turn:'):
            try:
                num = int(key.split(':', 1)[1])
                max_num = max(max_num, num)
            except (TypeError, ValueError):
                pass
    return f'turn:{max_num + 1}'
```

**逻辑**: 扫描所有 `role=user` 消息，取其 `_turn_key` 的最大编号 +1，作为下一个新 turn 的 key。

### 2.2 在用户提交消息时打戳

**文件**: `api/routes.py` 第 11808 行

当用户通过聊天提交一条新消息（POST 到 `/api/chat/start` 或类似路由），服务端构建 user message 时立即为其分配 `_turn_key`：

```python
from integration.session_manifest.manifest import _next_turn_key
user_msg["_turn_key"] = _next_turn_key(existing)
s.messages.append(user_msg)
```

此时消息结构形如：

```json
{
  "role": "user",
  "content": "用户说的内容",
  "_turn_key": "turn:3",
  "timestamp": 1781490430
}
```

### 2.3 当前轮在 merge 时绑定

`/api/chat/start` 在 session lock 内生成一次 `pending_turn_key`，worker 将它作为 `stream_turn_key` 使用。Agent 返回的当前 user 副本和 deferred 模式合成的 user 都在进入 display/context 去重前直接绑定该 key。若 WebUI 已 eager checkpoint，同 key 的 user 即使被 partial、tool 或 marker 隔开，也会折叠到 checkpoint，而不是在流结束时重新编号。

流式正常、内部 retry 和 exception retry 均不得扫描历史无 key user 并调用 `_next_turn_key()`；active turn 的归属只能来自 `stream_turn_key`。

### 2.4 存量会话兼容

**文件**: `integration/session_manifest/manifest.py` 第 921–951 行 `_ensure_turn_keys()`

当 `/api/session/manifest` 构建时调用 `_ensure_turn_keys()`，它只返回 Manifest 自有的消息副本，不修改 session，也不为混合 keyed/unkeyed transcript 猜号。完全没有 `_turn_key` 的旧会话仍由 `_message_turns()` 降级使用消息数组索引；混合会话的缺 key 行记录在 `diagnostics.missing_turn_key_message_indices`。

```python
def _ensure_turn_keys(messages: list) -> list:
    return copy.deepcopy(list(messages or []))
```

---

## 3. `/api/session` 接口中的 turn_key

### 3.1 接口入口

**路由**: `api/routes.py` 第 5489 行 `GET /api/session?session_id=...`

### 3.2 stream_turn_key 推导

**文件**: `api/routes.py` 第 11812–11826 行 `_turn_key_for_pending_user_message()`

在用户提交消息启动流式运行时，在获取 session lock 之后、启动 worker 线程之前，计算当前轮次的 turn_key：

```python
def _turn_key_for_pending_user_message(s, msg: str) -> str:
    messages = list(getattr(s, "messages", None) or [])
    if messages:
        latest = messages[-1]
        if isinstance(latest, dict) and latest.get("role") == "user":
            # 优先使用稳定的 _turn_key
            turn_key = latest.get("_turn_key", "")
            if turn_key:
                return turn_key
            # 降级：如果最后一条消息内容和当前提交相同，用索引
            row_text = " ".join(str(latest.get("content") or "").split())
            msg_text = " ".join(str(msg or "").split())
            if row_text == msg_text:
                return f"turn:{len(messages) - 1}"
    return f"turn:{len(messages)}"
```

**逻辑**:
1. 取 `s.messages` 中最后一条消息
2. 如果它是 `role=user` 且已有 `_turn_key`，直接返回
3. 否则降级为索引 key `turn:<idx>`

### 3.3 传入 worker 线程

**文件**: `api/routes.py` 第 12085 行

```python
worker_kwargs = {"model_provider": model_provider, "stream_turn_key": stream_turn_key}
```

`stream_turn_key` 被作为参数传入 `_run_agent_streaming()` 或 `_run_gateway_chat_streaming()`。

### 3.4 worker 中使用

**文件**: `api/streaming.py` 第 5268–5270 行

```python
_manifest_turn_key = str(stream_turn_key or s.pending_turn_key or '').strip()
```

worker 不从 transcript 长度推导当前轮。key 缺失时允许 transcript/error 状态落盘，但成果结算保持失败，不创建 guessed turn decision。

### 3.5 消息分页：`turn_align` 与 `msg_limit`

`GET /api/session?messages=1&msg_limit=N` 默认按**原始消息条数**截取尾部窗口，可能在 user / assistant / tool 链中间截断。

查询参数：

| 参数 | 说明 |
|------|------|
| `msg_limit=N` | 原始消息条数预算（含 tool 行）。未传时返回全量 transcript。 |
| `turn_align=1` | 与 `msg_limit` 配合使用：尾部窗口按**完整 user 轮次**对齐（边界与 `_message_turns()` 一致）。 |
| `expand_renderable=1` | 仅在**未**设置 `turn_align` 时生效：向后扩展窗口直至约 `msg_limit` 条可渲染行（user/assistant）。 |

`turn_align=1` 时的预算规则：

- 末轮消息数 **> N**：只返回末轮（整轮，可超过 N）。
- 末轮消息数 **≤ N**：从末轮向前累加完整轮次，直到再加一轮会使总条数 **> N**。

示例（`msg_limit=50`）：

- 末轮 60 条 → 返回 60 条（1 轮）。
- 末轮 20 条、上一轮 25 条、再上一轮 30 条 → 返回 45 条（末轮 + 上一轮）。

推荐调用：

```http
GET /api/session?session_id=...&messages=1&msg_limit=50&turn_align=1
```

响应字段 `_messages_truncated` 在 `turn_align=1` 时表示 `_messages_offset > 0`（仍有更早轮次未返回），而非简单的 `len(messages) > msg_limit`。

完整 HTTP 契约、参数组合与分页流程见 [session-message-pagination-api.md](./session-message-pagination-api.md)。

---

## 4. `/api/session/manifest` 接口中的 turn_key

本节只定义 turn 身份如何投影到 Manifest；公开 HTTP/SSE 字段、完整 JSON 示例、资源语义和
前端合并规则以 [Session Manifest HTTP/SSE 契约](../api/session-manifest-api.md) 为唯一来源。

### 4.1 接口入口

`api/routes.py` 调用 `build_session_manifest()`；若该会话有活跃 stream，则将其 live delta
合并后返回。`turn_key` 不由 HTTP 请求推导，而是由 transcript 中真实 user anchor 提供。

### 4.2 `build_session_manifest()` 流程

`build_session_manifest()` 加载 display transcript、按 user message 切轮、把 tool event 归属到
turn，并输出 per-turn 投影。Artifacts 的长期权威来源是 profile-aware
`session_manifest.db`；todos/references 仍从 transcript/tool events 派生。Artifact store 的身份
与决策规则见 [Session Manifest Artifacts 实现](session-manifest-artifacts.md)。

### 4.3 `_message_turns()` — 从消息推导 turns

**文件**: `integration/session_manifest/manifest.py` 第 954–973 行

核心函数，扫描所有 user 消息推导出 turn 列表：

```python
def _message_turns(messages: list) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for idx, message in enumerate(messages or []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        if turns:
            turns[-1]['end_msg_idx'] = idx - 1
        # 优先使用稳定的 _turn_key；没有时降级为索引 key
        turn_key = str(message.get('_turn_key', '') or '')
        if not turn_key:
            turn_key = f'turn:{idx}'
        turns.append({
            'turn_key': turn_key,
            'user_msg_idx': idx,
            'start_msg_idx': idx,
            'end_msg_idx': len(messages or []) - 1,  # 临时，后面会被下一条 user 修正
        })
    return turns
```

**关键规则**:
- 按 user 消息切分轮次
- **优先使用 user 消息的 `_turn_key` 字段**
- 没有 `_turn_key` 时降级为 `turn:<idx>`（idx 为消息数组索引）
- 每轮的 `end_msg_idx` = 下一条 user 消息的 idx - 1

### 4.4 turn 与 tool event 的关联：`_turn_key_for_event()`

**文件**: `integration/session_manifest/manifest.py` 第 976–987 行

```python
def _turn_key_for_event(event: ToolEvent, turns: list[dict[str, Any]]) -> str | None:
    idx = event.assistant_msg_idx
    if idx is None:
        idx = event.tool_msg_idx
    if isinstance(idx, bool) or not isinstance(idx, int):
        return None
    for turn in reversed(turns):
        start = turn.get('start_msg_idx')
        end = turn.get('end_msg_idx')
        if isinstance(start, int) and isinstance(end, int) and start <= idx <= end:
            return str(turn.get('turn_key') or '')
    return None
```

通过 tool event 的 `assistant_msg_idx` 或 `tool_msg_idx` 在 turn 的 `[start_msg_idx, end_msg_idx]` 范围内查找，将该 event 归属到对应的 turn。

### 4.5 turn 的序列化格式：`_turn_to_wire()`

`_turn_to_wire()` 输出该 turn 的 key、artifacts 与 references。字段定义和示例见 API 契约的
[`turns[]`](../api/session-manifest-api.md#turns按-user-turn-的局部投影) 一节。

### 4.6 Turn Reconcile（轮次交付物归因）

**`_apply_turn_reconcile_to_manifest_records()`**（`integration/session_manifest/manifest.py` 第 1269 行起）在整个 manifest 构建时，对每一轮执行 reconcile：

1. 对每个 turn 调用 `_turn_message_slice(messages, turn_key)` 切出该轮的消息切片
2. 收集该切片内的 tool events、MEDIA 标记、交付 prose
3. 将成果归入该 turn 的 `artifacts`（当前实现中 `references` 主要来自 `skill_view`）

**`reconcile_turn_artifact_events()`**（第 1289 行）对外暴露的单轮 reconcile 接口，SSE 流式的 `_emit_turn_complete_reconcile_delta()` 使用。

**turn 与 manifest 记录的归因规则**（新会话）：

| 步骤 | 行为 |
| --- | --- |
| 切轮 | `_message_turns()` 按 user 消息切分；`turn_key` 来自 `message._turn_key`，不是把 `turn:N` 里的 N 当数组下标 |
| 收集事件 | `_collect_tool_events()` + `_collect_media_artifact_events()` 从 transcript / `session.tool_calls` 提取 tool 活动 |
| 归 turn | `_turn_key_for_event()` 用 event 的 `assistant_msg_idx` / `tool_msg_idx` 落在哪个 turn 的 `[start_msg_idx, end_msg_idx]` 区间来归属 |
| 写入 | 写入类工具 → `turns[].artifacts`；`skill_view` → `turns[].references`；`read_file` / `glob` / `grep` 等**不**进入 references |
| 补漏 | reconcile 从该轮 transcript 补 workspace 内真实存在的文件到 `artifacts` |

---

## 5. 流式 SSE 中的 turn_key (`manifest_delta`)

### 5.1 `_emit_manifest_delta()` — 工具事件实时推送

工具调用完成后，`_emit_manifest_delta()` 使用当前 `_manifest_turn_key` 产生 delta。开始事件
不产生 artifact/reference；完整事件外壳与字段约束见 API 契约的
[`manifest_delta`](../api/session-manifest-api.md#3-sse-manifest_delta) 一节。

### 5.2 `_emit_turn_complete_reconcile_delta()` — 轮次完成后 reconcile

整轮结束后，`_emit_turn_complete_reconcile_delta()` 使用同一个 key 作一次 transcript reconcile。
因此前端能把工具完成与 turn-complete delta 归入同一轮；该 delta 的字段形状仍由 API 契约定义。

---

## 6. 前端消费 turn_key

### 6.1 从 session 消息中提取

渲染助手回复时，`static/ui.js` 从对应 user message 读取 `_turn_key` 并写入
`data-turn-key`；仅历史无 key transcript 才按 API 契约的 fallback 规则处理。

### 6.2 从 manifest 中获取 per-turn 成果

`static/workspace.js` 按相同 key 查询 `manifest.turns[]`，聊天区只消费该 turn 的 artifacts。

### 6.3 SSE manifest_delta 合并

收到 `manifest_delta` 后，前端按其 `turn_key` 将局部行并入缓存；合并/幂等规则不在此处重复，
以 API 契约为准。

---

## 7. 完整数据流总结

```
用户提交消息
    │
    ▼
api/routes.py: _turn_key_for_pending_user_message()
    │  ← 从 s.messages[-1]._turn_key 推导
    │  赋值为 stream_turn_key
    ▼
api/routes.py: 启动 worker(stream_turn_key=stream_turn_key)
    │
    ▼
api/streaming.py: _manifest_turn_key = stream_turn_key
    │
    ├─▶ _emit_manifest_delta(turn_key=_manifest_turn_key)
    │      SSE → 前端 workspace.js: applySessionManifestDelta()
    │
    ├─▶ _emit_turn_complete_reconcile_delta(turn_key=_manifest_turn_key)
    │      SSE → 前端 workspace.js: applySessionManifestDelta()
    │
    └─▶ merge 时将 stream_turn_key 绑定到当前 user
           keyed eager/Agent duplicate 折叠为一行并持久化
    │
    ▼
integration/session_manifest/manifest.py: build_session_manifest()
    │  ← _ensure_turn_keys() 复制消息，不猜 active turn 编号
    │  ← _message_turns() 按 user msg 切分 turn，优先使用 _turn_key
    │  ← _turn_key_for_event() 将 tool events 归入对应 turn
    │  ← 输出 turns[] 到 manifest 响应
    ▼
GET /api/session/manifest?session_id=...
    → 按 API 契约返回各 `turn_key` 的 artifacts/references 局部投影
    │
    ▼
前端 ui.js: 从 session.messages[]._turn_key 取 key
前端 workspace.js: getTurnArtifacts(turn_key) 查 manifest
    → 渲染 per-turn 成果 chip
```

### 关键设计要点

1. **稳定性优先** — `_turn_key` 一旦生成并持久化到消息中，在**仍保留该 user 消息**的前提下不会改变。被动压缩保留完整 display transcript，因此所有 `_turn_key` 通常仍在；主动压缩会删除 middle 区域的 user 消息，对应 `_turn_key` 与 per-turn manifest 一并丢失（见第 8 节）。

2. **降级兼容** — 存量会话没有 `_turn_key` 字段时，自动降级为 `turn:<数组索引>`，确保不影响历史数据展示。

3. **双向关联** — turn_key 同时在消息层（`message._turn_key`）和 manifest 层（`manifest.turns[].turn_key`）存在。消息层提供 origin（谁是第 N 轮的用户消息），manifest 层提供 per-turn 聚合产物。

4. **SSE 一致性** — 流式过程中所有 `manifest_delta` 携带同一个 `_manifest_turn_key`，前端据此将实时增量归入正确的轮次，并与最终的持久化 manifest 对齐。

5. **结算失败关闭** — 当前轮持久化前必须满足最新真实 user 的 `_turn_key == stream_turn_key`。冲突时不写 artifact decision；store 中没有 user anchor 的旧记录只进入顶层 artifacts 与 `diagnostics.orphan_turn_keys`，不伪装成正常 `turns[]`。

---

## 8. 压缩与 turn_key

本节说明**新会话**在主动/被动、轻度/重度压缩下，`turn_key` 能否与 UI / manifest 对齐。更完整的会话分裂与压缩场景见 [`docs/architecture/multi-turn-conversation.md`](multi-turn-conversation.md)。

### 8.1 两类压缩

| 类型 | 触发 | 入口 | `s.messages` 更新方式 |
| --- | --- | --- | --- |
| **主动压缩** | 用户 `/compress` 或 `POST /api/session/compress` | `api/routes.py` `_handle_session_compress()` | **直接替换** `s.messages = compressed` |
| **被动压缩** | Agent 上下文超阈值，流式运行中自动触发 | `hermes-agent` `ContextCompressor` + `api/streaming.py` | **合并追加** `_merge_display_messages_after_agent_result()` |

两类压缩共用同一套 `ContextCompressor.compress()`（`hermes-agent/agent/context_compressor.py`），但 WebUI 对 display transcript 的处理方式不同，因此对 `_turn_key` 的影响也不同。

### 8.2 压缩器如何改写 messages

`ContextCompressor.compress()` 将 transcript 分为三段：

```
┌──────────┐   ┌──────────────┐   ┌──────────┐
│  HEAD    │   │   MIDDLE     │   │  TAIL    │
│ 保护前 N │   │ LLM 摘要替换  │   │ token 预算 │
└──────────┘   └──────────────┘   └──────────┘
 shallow copy      合成摘要消息       shallow copy
 _turn_key 保留     无 _turn_key       _turn_key 保留
```

核心代码（`context_compressor.py` 第 1476–1556 行）：

- **Head**：`messages[i].copy()`，user 消息上的 `_turn_key` 原样保留
- **Middle**：中间轮次整条删除，替换为一条 `[CONTEXT COMPACTION — REFERENCE ONLY]` 摘要；该合成消息**没有** `_turn_key`
- **Tail**：最近约 `tail_token_budget` tokens 内的消息 shallow copy，`_turn_key` 保留

`tail_token_budget` 默认约为 `context_length × threshold_percent(50%) × summary_target_ratio(20%)`（大上下文模型约 20K tokens）。

### 8.3 主动压缩对 turn_key 的影响

`_handle_session_compress()`（`api/routes.py` 第 14377–14379 行）在压缩完成后：

```python
s.messages = compressed
s.context_messages = compressed
s.tool_calls = []
```

**后果**：

- Head / Tail 中**仍存在的** user 消息：`_turn_key` 不变（如 `turn:1`、`turn:10`）✓
- Middle 中被摘要掉的 user 消息：**整条消失**，`_turn_key` 永久丢失 ✗
- 压缩后 `build_session_manifest()` 只能为**仍留在 transcript 中的 user 消息**重建 `turns[]`
- 被压缩轮次的 tool 链若已从 transcript 移除，对应 `turns[].artifacts` 通常也为空（workspace 内真实文件仍在，只是 manifest 索引丢失）
- 摘要消息即使采用 `role=user` 也不会由 `_ensure_turn_keys()` 补戳；Manifest 切轮会继续跳过 context compression marker

**示例**（10 轮对话，重度主动压缩）：

```
压缩前: [sys, user(turn:1), ..., user(turn:10)]
压缩后: [sys, user(turn:1), [SUMMARY], user(turn:10)]
        turn:2..turn:9 的 user 消息与 _turn_key 均丢失
```

### 8.4 被动压缩对 turn_key 的影响

流式结束后（`api/streaming.py` 第 6366–6377 行），display 与 context 分开更新：

```python
s.context_messages = ...   # 模型视角，可为压缩后的较短历史
s.messages = _merge_display_messages_after_agent_result(
    _previous_messages, _previous_context_messages, _result_messages, msg_text,
    canonical_turn_key=_manifest_turn_key,
)
```

`_merge_display_messages_after_agent_result()`（第 3684 行起）在检测到 context 被压缩（`result_messages` 不再是 `previous_context` 的前缀）时：

```python
merged = previous_display[:]          # 以旧 display 为骨架，不删旧轮
candidates = marker_candidates + turn_candidates   # 仅追加压缩标记 + 当前轮
merged += candidates
```

**后果**：

- **所有历史 user 消息的 `_turn_key` 保留** ✓（display 层不裁减中间轮次）
- 压缩仅缩短 `s.context_messages`（模型上下文）；manifest 从 `_load_display_messages()` / `s.messages` 构建，仍能看到全部 turn
- 流式 SSE 的 `_manifest_turn_key` 仍对应当前轮的 `stream_turn_key`，与压缩无关
- 压缩完成后推送 `compressed` SSE，并设置 `compression_anchor_*` 元数据（`streaming.py` 第 6740–6817 行）

**示例**（被动压缩后 display）：

```
s.messages = [全部旧 user/asst/tool] + [[CONTEXT COMPACTION]] + [当前轮 user + asst]
             ↑ 所有 turn:1..turn:N 的 _turn_key 仍在
```

### 8.5 轻度 vs 重度压缩

「轻度」「重度」不是独立开关，而是由**同一算法**下 middle 区域大小决定：middle 越大、tail 预算越小，被摘要掉的轮次越多。

| 程度 | 典型表现 | Head `_turn_key` | Middle `_turn_key` | Tail `_turn_key` |
| --- | --- | --- | --- | --- |
| 轻度 | middle 少、tail 大 | 保留 ✓ | 主动：少量丢失；被动：保留 ✓ | 保留 ✓ |
| 重度 | middle 多、tail 小 | 保留 ✓ | 主动：大量丢失 ✗；被动：保留 ✓ | 主动：仅最近几轮保留 ○；被动：保留 ✓ |

Head 始终受 `protect_first_n`（默认 3，外加 system prompt）保护，最早几轮的 `_turn_key` 在主动压缩下也通常仍在。

### 8.6 压缩后 manifest 与前端对齐

| 链路 | 主动压缩后 | 被动压缩后 |
| --- | --- | --- |
| `ui.js` `dataset.turnKey` | 仍从 user 消息的 `_turn_key` 读取；被删轮次的 DOM 不再存在 | 全部轮次仍可对齐 ✓ |
| `getTurnArtifacts(turnKey)` | 仅存活 turn 有 manifest 行；丢失 turn 返回 `[]` | 全部 turn 可匹配 ✓ |
| 新 turn 编号 | chat-start 生成一次 max+1 并绑定 worker；历史删除可产生缺号 | 同左 |

测试覆盖：

- `tests/test_session_manifest.py::test_build_session_manifest_compression_turn_keys` — 中间插入压缩标记后，`turn:1` / `turn:2` 稳定，且 `turn:1` 的 artifacts 仍正确归属。
- `tests/test_artifact_turn_isolation.py::test_passive_compression_rotation_keeps_canonical_turn_artifact_alignment` — 同一 active turn 内发生被动压缩并旋转到 continuation 后，Agent user 回显仍折叠到 canonical key；父段与当前轮 artifacts 均可由 continuation Manifest 按原 turn 读取，且不产生 orphan。

### 8.7 压缩场景总结

```
                    ┌───────────────┬───────────────┬───────────────┐
                    │ Head turn_key │ Middle turn_key│ Tail turn_key │
├───────────────────┼───────────────┼───────────────┼───────────────┤
│ 主动压缩（任意）   │    保留 ✓     │    丢失 ✗     │  保留 ✓/部分 ○ │
│ 被动压缩（任意）   │    保留 ✓     │    保留 ✓     │    保留 ✓     │
└───────────────────┴───────────────┴───────────────┴───────────────┘
```

**一句话**：被动压缩对 turn_key **基本无害**；主动压缩会永久删除 middle 轮次的 user 消息及其 `_turn_key`，仅 head/tail 存活轮次仍能与 manifest / 聊天区 chip 对齐。新 turn 只在 chat-start 分配一次，编号允许有缺号。

### 8.8 压缩延续（session_id 旋转）

当 Agent 判定上下文**无法继续压缩**时，可能创建 **continuation 会话**（`session_id` 变化，`streaming.py` 第 6414 行起）。此时：

- 旧会话标记为 `pre_compression_snapshot`，完整历史保留在父会话 JSON
- **当前会话**的 artifact manifest 读取同一 compression `lineage_key` 下、同 profile 的 store records；普通 fork 的 parent 不参与合并
- 跨 continuation 的历史通过 `parent_session_id` 链拼接展示；artifact store 会按 compression lineage 补齐父段 artifacts，普通 fork 不补齐
- 当前 continuation 内的新 user 消息仍按 `_next_turn_key()` 独立编号

详见 [`docs/architecture/multi-turn-conversation.md`](multi-turn-conversation.md) 第 6.2–6.4 节。
