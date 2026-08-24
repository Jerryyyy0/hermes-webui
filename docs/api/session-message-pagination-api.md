# `/api/session` 消息分页接口说明

本文档描述 `GET /api/session` 在加载会话 transcript 时的**尾部窗口分页**契约，重点说明 `msg_limit`、`turn_align` 与 `expand_renderable` 的语义与组合方式。

实现入口：`api/routes.py`（`GET /api/session`、`_message_window_for_display`、`_turn_aligned_window_indices`）；轮次边界与 [`_message_turns()`](integration/session_manifest/manifest.py) 一致。轮次 key 背景见 [turn-key-backend.md](./turn-key-backend.md)。

---

## 1. 接口概览

### `GET /api/session`

在需要 transcript 时，客户端传 `messages=1`（默认即为 `1`）。若同时传 `msg_limit`，响应中的 `session.messages` 仅为合并后全量 transcript 的一个**尾部窗口**；更早内容通过增大 `msg_limit` 或配合 `msg_before` 再请求。

#### 与分页相关的查询参数

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `session_id` | 是 | 会话 ID |
| `messages` | 否 | `0` 跳过消息体（仅元数据）；默认 `1` 加载消息 |
| `msg_limit` | 否 | 原始消息条数预算（含 `role=tool` 行）。**未传时返回全量** `messages` |
| `turn_align` | 否 | `1` 或 `true`：与 `msg_limit` 配合，按**完整 user 轮次**对齐尾部窗口 |
| `expand_renderable` | 否 | `1` 或 `true`：仅在**未**设置 `turn_align` 时生效，向后扩展直至约 `msg_limit` 条可渲染行 |
| `msg_before` | 否 | 0-based 索引；仅在 `messages[0..msg_before)` 上计算窗口（用于向前分页） |
| `resolve_model` | 否 | 消息加载时默认 `1`；`messages=0` 时默认 `0` |

#### 分页相关响应字段（`session` 对象内）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `messages` | `array` | 当前窗口内的消息行 |
| `message_count` | `int` | **全量**合并 transcript 的消息条数（非窗口长度） |
| `_messages_offset` | `int` | 窗口第一条消息在全量 `messages` 数组中的 0-based 索引 |
| `_messages_truncated` | `bool` | 是否仍有未返回的更早消息 |

其他字段（`tool_calls`、`pending_user_message`、`context_length` 等）与分页无直接关系；`tool_calls` 在窗口化时会按 `_messages_offset` 重基到窗口内索引。

#### 错误响应

| 状态码 | 条件 |
| --- | --- |
| `400` | 缺少 `session_id` |
| `404` | 会话不存在 |

---

## 2. 轮次（Turn）定义

分页中的「一轮」与 Session Manifest 相同：

- 每条 `role=user` 消息开启一轮（**上下文压缩 marker** 除外）。
- 一轮包含：该 user 消息 + 直至下一条 user 之前的所有 assistant / tool 等消息。
- 存量消息无 `_turn_key` 时，仍按 user 索引切分；有 `_turn_key` 时不改变分页边界。

---

## 3. `msg_limit`（默认尾部切片）

未传 `turn_align` 时，`msg_limit=N` 表示：在有效 source（见 §6）上取**最后 N 条原始消息**。

```
start_idx = max(0, len(source) - N)
window    = source[start_idx : len(source)]
```

特点：

- 可能在 user / assistant / tool 链**中间**截断。
- 若窗口内没有任何可渲染行（例如尾部全是 hidden tool 行），会将窗口末端回退到最近一条可渲染消息，再重新按 N 条 raw 切片。
- `_messages_truncated`：`len(全量 messages) > msg_limit`（或 `msg_before` 场景下 `len(slice) > msg_limit`）。

---

## 4. `turn_align=1`（轮次对齐窗口）

与 `msg_limit` **必须同时出现**才生效；单独传 `turn_align` 而无 `msg_limit` 时无额外行为（仍返回全量或走无 limit 路径）。

`msg_limit=N` 在此模式下为**原始消息条数预算**（含 tool 行），从**最后一轮**向前贪心累加**完整轮次**：

| 情况 | 返回 |
| --- | --- |
| 末轮消息数 **> N** | 仅返回末轮（整轮，条数可 **超过 N**） |
| 末轮消息数 **≤ N** | 从末轮向前继续加整轮，直到再加一轮会使总条数 **> N** |

**不变量**：`messages` 窗口是 source 上的连续切片 `[start_idx, end_idx)`，且 `start_idx` 为某一轮的 `start_msg_idx`。

### 示例（`msg_limit=50`）

| 末轮 | 上一轮 | 再上一轮 | 返回 |
| --- | --- | --- | --- |
| 60 条 | 25 条 | 30 条 | 60 条（仅末轮） |
| 20 条 | 25 条 | 30 条 | 45 条（末轮 + 上一轮） |

### `_messages_truncated`（`turn_align` 路径）

与 raw 路径不同：

```
_messages_truncated = (_messages_offset > 0)
```

即：只要窗口不是从 transcript 开头开始，即视为仍有更早内容；**不因末轮超过 N 而单独置 true**。

### 推荐调用

```http
GET /api/session?session_id=20260617_123834_2f0ed6&messages=1&msg_limit=50&turn_align=1
```

### 与 `expand_renderable` 的关系

`turn_align=1` 时 **不执行** `expand_renderable` 逻辑（二者互不影响；可同 URL 传参，但 expand 被忽略）。

### 边界情况

| 场景 | 行为 |
| --- | --- |
| 无 `role=user`（纯 tool 等） | 回退为 raw tail：`max(0, len - N)` |
| 窗口内无可渲染行 | 将 source 截断到最近可渲染行后，再按轮次规则重算窗口 |
| 压缩 marker 与 orphan 行 | 连续选取多轮时，切片覆盖两轮之间的 compression summary 等行；仅末轮时 marker 之前的 orphan 不返回 |
| 主动压缩删除的 middle 轮次 | 无法再通过分页加载（与全量 transcript 一致） |

---

## 5. `expand_renderable=1`（可渲染行扩展）

**仅在未设置 `turn_align`** 且存在 `msg_limit` 时生效。

WebUI 渲染时会过滤 `role=tool`、空 partial activity 等行。工具密集的尾部可能 raw 已满 `N` 条，但可见只有 1～2 条 user/assistant。`expand_renderable=1` 在 raw 尾部窗口算出后，**向前扩展**直到窗口内约有 `msg_limit` 条**可渲染**行（user/assistant，不含 tool）。

可渲染行判定：有 `role` 且不是 `tool`，且不是空的 partial activity 消息。

典型用途：WebUI **首次冷加载**（`_ensureMessagesLoaded`）。「加载更早消息」与 `msg_before` 分页**不应**传此参数，以免一次拉回过多 tool-heavy 历史。

---

## 6. `msg_before`（向前分页锚点）

`msg_before=B` 时，先在全量合并 transcript 上取 `source = messages[0:B]`，再在该 prefix 上应用 `msg_limit`（及可选的 `turn_align` / `expand_renderable`）。

用于：在索引 `B` 之前取一页尾部窗口。与 `turn_align=1` 组合时，轮次对齐规则作用于 prefix，而非全量 transcript 的物理尾部。

---

## 7. 客户端分页流程参考

### 7.1 首次加载（轮次对齐）

```http
GET /api/session?session_id={sid}&messages=1&resolve_model=0&msg_limit=50&turn_align=1
```

记录：

- `S.messages = session.messages`
- `_oldestIdx = session._messages_offset`
- `_messagesTruncated = session._messages_truncated`

### 7.2 加载更早（增大 `msg_limit`）

在同一 contract 下请求更大的 `msg_limit`（带 `turn_align=1`）。若更大预算仍无法纳入「上一整轮」（例如当前末轮 44 条、上一轮 100 条、预算 74），累加请求不会拉长窗口；WebUI 会自动改走 `msg_before={_oldestIdx}` 在 prefix 上按轮次取更早一页。

### 7.3 `msg_before` 回退页

```http
GET /api/session?session_id={sid}&messages=1&resolve_model=0&msg_before={_oldestIdx}&msg_limit=50&turn_align=1
```

### 7.4 WebUI 默认行为

| 场景 | `msg_limit` | `turn_align` | `expand_renderable` |
| --- | --- | --- | --- |
| 冷启动（`_ensureMessagesLoaded`） | `30`（`_INITIAL_MSG_LIMIT`） | `1` | 不传（`turn_align` 路径下后端忽略 expand） |
| 上拉加载更早（`_loadOlderMessages`） | 累加 `当前条数 + 30` | `1` | 不传 |
| `msg_before` 回退页（上拉 race fallback） | `30` | `1` | 不传 |

---

## 8. 参数组合速查

| `msg_limit` | `turn_align` | `expand_renderable` | 窗口策略 |
| --- | --- | --- | --- |
| 未传 | — | — | 全量 `messages` |
| N | 未传 / `0` | 未传 / `0` | 最后 N 条 raw |
| N | 未传 / `0` | `1` | 最后 N 条 raw，再按可渲染行向前扩 |
| N | `1` | 任意 | 轮次对齐预算 N；expand **不生效** |

---

## 9. 实现索引

| 符号 | 位置 |
| --- | --- |
| `GET /api/session` 参数解析 | `api/routes.py` |
| `_turn_aligned_window_indices` | `api/routes.py` |
| `_message_window_for_display` | `api/routes.py` |
| `_message_turns` | `integration/session_manifest/manifest.py` |
| 单测 | `tests/test_session_message_window_turn_align.py`、`tests/test_session_message_window_renderable_tail.py` |
