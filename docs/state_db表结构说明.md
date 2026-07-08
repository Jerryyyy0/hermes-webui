# Hermes `state.db` 表结构说明

> 本文描述 Hermes Agent 的会话数据库 `state.db` 的表结构、字段语义、索引、约束与版本演进。
>
> **权威来源**：
> - 代码：`hermes-agent/hermes_state.py` 的 `SCHEMA_SQL` 常量（`SCHEMA_VERSION = 17`）
> - 文档：`hermes-agent/website/docs/developer-guide/session-storage.md`
>
> WebUI 通过 `api/state_sync.py` 把会话元数据镜像写入此 DB（受 `sync_to_insights` 设置控制，默认关）；通过 `api/models.py::_active_state_db_path()` 解析当前 profile 的 DB 路径。

---

## 1. 概览

| 项目 | 值 |
|------|----|
| 文件路径 | `~/.hermes/state.db`（默认）/ `$HERMES_HOME/state.db`（覆盖）|
| 引擎 | SQLite，WAL 模式 |
| 当前 schema 版本 | **17** |
| 配套文件 | `state.db-wal`（预写日志）、`state.db-shm`（共享内存）|

**表清单**

| 类型 | 名称 | 作用 |
|------|------|------|
| 普通表 | `sessions` | 会话元数据、token 统计、计费、handoff/compression 状态 |
| 普通表 | `messages` | 完整消息历史（用户轮、助手回复、工具调用、压缩/重放标记等） |
| 普通表 | `state_meta` | KV 元数据（schema 版本标记、telegram topic 版本等） |
| 普通表 | `compression_locks` | 上下文压缩互斥锁（每个 session 至多一行） |
| 普通表 | `schema_version` | 单行表，存当前 schema 版本号 |
| 条件表 | `telegram_dm_topic_mode` | Telegram DM topic 模式状态（启用 telegram topic 时才用） |
| 条件表 | `telegram_dm_topic_bindings` | Telegram DM topic ↔ session 绑定 |
| 虚拟表 | `messages_fts` | FTS5 全文索引，覆盖 `content + tool_name + tool_calls` |
| 虚拟表 | `messages_fts_trigram` | FTS5 三元组索引，用于 CJK / 子串搜索 |

---

## 2. `sessions` — 会话元数据

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | TEXT | ✅ | 会话唯一 ID（主键） |
| `source` | TEXT | ✅ | 来源标签：`cli` / `telegram` / `discord` / `webui` / `cron` 等，用于侧栏筛选与 source-of-truth 路由 |
| `user_id` | TEXT |  | 用户/平台账号标识（gateway 平台下使用） |
| `session_key` | TEXT |  | 稳定业务键（如 cron job 的复合键、telegram chat 标识） |
| `chat_id` | TEXT |  | Gateway 平台 chat id |
| `chat_type` | TEXT |  | Gateway 平台 chat 类型（private / group / channel …） |
| `thread_id` | TEXT |  | Telegram topic / Slack thread 等子会话 id |
| `model` | TEXT |  | 模型 slug（如 `anthropic/claude-sonnet-4.6`） |
| `model_config` | TEXT |  | JSON 字符串，模型/Agent 运行时配置；v16 起可能内嵌 `_delegate_from` 子代理标记 |
| `system_prompt` | TEXT |  | 系统提示词 |
| `parent_session_id` | TEXT |  | 父会话 id（上下文压缩触发分叉时链接前一段），形成 lineage 链 |
| `started_at` | REAL | ✅ | 起始时间，Unix epoch float（`time.time()`） |
| `ended_at` | REAL |  | 结束时间；为 NULL 表示活动会话 |
| `end_reason` | TEXT |  | 结束原因（`user_exit` / `compression` / `error` 等） |
| `message_count` | INTEGER |  | 消息条数（缓存值） |
| `tool_call_count` | INTEGER |  | 工具调用条数（缓存值） |
| `input_tokens` | INTEGER |  | 累计输入 token |
| `output_tokens` | INTEGER |  | 累计输出 token |
| `cache_read_tokens` | INTEGER |  | Anthropic prompt cache 命中读 token |
| `cache_write_tokens` | INTEGER |  | Anthropic prompt cache 写入 token |
| `reasoning_tokens` | INTEGER |  | 思考 token（OpenAI o-series 等） |
| `cwd` | TEXT |  | CLI/gateway 启动时的工作目录 |
| `git_branch` | TEXT |  | 启动时的 git 分支 |
| `git_repo_root` | TEXT |  | 启动时的 git 仓库根路径 |
| `billing_provider` | TEXT |  | 计费 provider（如 `anthropic` / `openai`） |
| `billing_base_url` | TEXT |  | 实际请求的 base URL |
| `billing_mode` | TEXT |  | 计费模式（`api` / `oauth` / `subscription` 等） |
| `estimated_cost_usd` | REAL |  | 预估成本（USD） |
| `actual_cost_usd` | REAL |  | 实际成本（USD，provider 明确返回时） |
| `cost_status` | TEXT |  | 成本状态（`estimated` / `actual` / `unknown`） |
| `cost_source` | TEXT |  | 成本数据来源（`pricing_table` / `provider_response` 等） |
| `pricing_version` | TEXT |  | 使用的定价表版本 |
| `title` | TEXT |  | 会话标题（用户可改） |
| `api_call_count` | INTEGER |  | API 调用次数 |
| `handoff_state` | TEXT |  | 交接状态（如 `pending` / `delivered` / `failed`） |
| `handoff_platform` | TEXT |  | 交接目标平台 |
| `handoff_error` | TEXT |  | 交接失败原因 |
| `compression_failure_cooldown_until` | REAL |  | 压缩失败冷却到期时间，避免连续重试 |
| `compression_failure_error` | TEXT |  | 最近一次压缩失败原因 |
| `rewind_count` | INTEGER |  | 撤销/回退次数 |
| `archived` | INTEGER |  | 是否归档（0/1，默认 0） |

### 索引

| 名称 | 字段 | 用途 |
|------|------|------|
| `idx_sessions_source` | `source` | 按 source 筛选 |
| `idx_sessions_source_id` | `source, id` | source+id 复合查询 |
| `idx_sessions_parent` | `parent_session_id` | lineage 反向查询（子→父） |
| `idx_sessions_started` | `started_at DESC` | 时间倒序列表 |
| `idx_sessions_session_key` | `session_key, started_at DESC` | 业务键查找最新段 |
| `idx_sessions_gateway_peer` | `source, user_id, chat_id, chat_type, thread_id, started_at DESC` | gateway 平台 peer 查找 |
| `idx_sessions_handoff_state` | `handoff_state, started_at` | 交接队列 |

---

## 3. `messages` — 消息历史

```sql
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0
);
```

### 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | INTEGER | ✅ | 自增主键 |
| `session_id` | TEXT | ✅ | 所属会话 id（外键 → `sessions.id`） |
| `role` | TEXT | ✅ | 消息角色：`user` / `assistant` / `tool` / `system` |
| `content` | TEXT |  | 消息文本内容（多模态内容序列化为 JSON 字符串） |
| `tool_call_id` | TEXT |  | 工具调用 id（角色为 `tool` 时对应原始 `tool_calls[].id`） |
| `tool_calls` | TEXT |  | **JSON 字符串**，工具调用列表 |
| `tool_name` | TEXT |  | 工具名（`tool` 角色或 assistant 工具调用） |
| `timestamp` | REAL | ✅ | Unix epoch float |
| `token_count` | INTEGER |  | 本条消息 token 数 |
| `finish_reason` | TEXT |  | LLM finish reason（`stop` / `tool_calls` / `length` …） |
| `reasoning` | TEXT |  | 原始推理文本（部分 provider 暴露） |
| `reasoning_content` | TEXT |  | DeepSeek/Anthropic 等的 reasoning content |
| `reasoning_details` | TEXT |  | **JSON 字符串**，结构化推理块 |
| `codex_reasoning_items` | TEXT |  | **JSON 字符串**，Codex Responses reasoning items 回放 |
| `codex_message_items` | TEXT |  | **JSON 字符串**，Codex Responses message id/phase 回放 |
| `platform_message_id` | TEXT |  | Gateway 平台消息 id（telegram/discord 等） |
| `observed` | INTEGER |  | 是否已被前端"观察到"（0/1） |
| `active` | INTEGER | ✅ | 软删除标记（0=hidden，1=visible），rewind/undo 用，默认 1 |
| `compacted` | INTEGER | ✅ | 是否被上下文压缩折叠掉（0/1），默认 0 |

### 索引

| 名称 | 字段 | 用途 |
|------|------|------|
| `idx_messages_session` | `session_id, timestamp` | 单会话按时间顺序读取 |
| `idx_messages_session_active` | `session_id, active, timestamp` | 只读 `active=1` 的可见消息，避免全表扫过滤 |
| `idx_messages_platform_msg_id` | `session_id, platform_message_id` WHERE `platform_message_id IS NOT NULL` | 部分索引，gateway 平台消息 id 反查 |

### 注意点

- `tool_calls` / `reasoning_details` / `codex_reasoning_items` / `codex_message_items` 都是 **JSON 字符串**，读取时需 `json.loads`。
- `active=0` 的行在 WebUI 渲染、上下文构造、token 统计等几乎所有读取路径中都会被过滤；只在历史审计/修复工具中读取。
- `compacted=1` 表示该消息已被后续的上下文压缩折叠，不再参与新一次 API 请求的上下文构造，但保留为历史可读。
- WebUI 渲染时如果发现 `state.db` 在可见 assistant tail 之后追加了隐藏 tool 行，会把窗口末端回退到最新可渲染行（见 `api/routes.py::_tail_renderable_window`）。

---

## 4. `state_meta` — KV 元数据

```sql
CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

| 用途 | key 示例 |
|------|---------|
| Schema 版本标记 | `telegram_dm_topic_schema_version`（独立子模块版本） |
| 其它子系统自管的 KV | 由各子模块自行写入 |

> 主 schema 版本走单独的 `schema_version` 表（见下），`state_meta` 只承担子模块自管的版本/标志位。

---

## 5. `compression_locks` — 压缩互斥锁

```sql
CREATE TABLE IF NOT EXISTS compression_locks (
    session_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | TEXT | 锁定的会话 id（主键，一个 session 至多一把锁） |
| `holder` | TEXT | 持锁者标识（进程/线程名） |
| `acquired_at` | REAL | 获取时间 |
| `expires_at` | REAL | 过期时间（超时后视为失效，可被抢占） |

索引：`idx_compression_locks_expires ON compression_locks(expires_at)`，用于清理过期锁。

---

## 6. `schema_version` — Schema 版本

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
```

单行表，存当前 schema 版本号。当前值：**17**。

- 普通列新增走 `_reconcile_columns()` 声明式 `ADD COLUMN`，不依赖版本号。
- 版本号仅在需要数据迁移（行回填、索引/FTS 重建、表结构变更）时才 bump。

---

## 7. `messages_fts` / `messages_fts_trigram` — FTS5 虚拟表

```sql
-- 基础 FTS5：unicode61 分词
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content);

-- 三元组 FTS5：CJK / 子串搜索
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);
```

两张表都通过 `AFTER INSERT/UPDATE/DELETE` 触发器与 `messages` 同步，索引内容为：

```sql
COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
```

（v11 起；早期版本只索引 `content`，并使用 external-content 模式，v11 后改为 inline 模式。）

### 何时用哪张

| 场景 | 用哪张 |
|------|--------|
| 英文/拉丁文关键词搜索（`docker deployment`、`"exact phrase"`、`deploy*`） | `messages_fts` |
| 中文/CJK/子串搜索（中文短语、品牌名片段等） | `messages_fts_trigram` |
| 当前 SQLite 构建不支持 FTS5 | 两表都不会创建；search 会回退到 LIKE 查询 |

---

## 8. Telegram Topic 表（条件创建）

仅当启用 Telegram DM topic 模式时才会创建；不影响其它 source。

### `telegram_dm_topic_mode`

```sql
CREATE TABLE IF NOT EXISTS telegram_dm_topic_mode (
    chat_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    activated_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    has_topics_enabled INTEGER,
    allows_users_to_create_topics INTEGER,
    capability_checked_at REAL,
    intro_message_id TEXT,
    pinned_message_id TEXT
);
```

### `telegram_dm_topic_bindings`

```sql
CREATE TABLE IF NOT EXISTS telegram_dm_topic_bindings (
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    managed_mode TEXT NOT NULL DEFAULT 'auto',
    linked_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (chat_id, thread_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_dm_topic_bindings_session
    ON telegram_dm_topic_bindings(session_id);

CREATE INDEX IF NOT EXISTS idx_telegram_dm_topic_bindings_user
    ON telegram_dm_topic_bindings(user_id, chat_id);
```

子模块自带版本控制（`state_meta.telegram_dm_topic_schema_version`，当前为 2）。v2 重建了 `telegram_dm_topic_bindings` 以加上 `ON DELETE CASCADE` 外键。

---

## 9. Schema 版本演进（含数据迁移）

> 普通列新增不在这里——`_reconcile_columns()` 会自动 `ADD COLUMN`。下表只列**需要数据迁移或表/索引结构变更**的版本。

| 版本 | 变更内容 |
|------|---------|
| 1 | 初始 schema（`sessions`、`messages`、FTS5） |
| 2 | `messages` 新增 `finish_reason`（声明式） |
| 3 | `sessions` 新增 `title`（声明式） |
| 4 | 在 `title` 上加唯一索引（NULL 允许，非 NULL 必须唯一） |
| 5 | 计费列：`cache_read_tokens`、`cache_write_tokens`、`reasoning_tokens`、`billing_provider`、`billing_base_url`、`billing_mode`、`estimated_cost_usd`、`actual_cost_usd`、`cost_status`、`cost_source`、`pricing_version`（声明式） |
| 6 | `messages` reasoning 列：`reasoning`、`reasoning_details`、`codex_reasoning_items`（声明式） |
| 7 | `messages` 新增 `reasoning_content`（声明式） |
| 8 | `sessions` 新增 `api_call_count`（声明式） |
| 9 | `messages` 新增 `codex_message_items`（Codex Responses message id/phase 回放） |
| 10 | 新增 `messages_fts_trigram` 虚拟表（trigram tokenizer，CJK/子串搜索）+ 旧数据回填 |
| 11 | 重建 `messages_fts` 与 `messages_fts_trigram`：覆盖 `tool_name + tool_calls`，从 external-content 切到 inline 模式；丢弃旧触发器，全量回填（修 #16751） |
| 12 | `messages.active` 软删除标记（rewind/undo 用）；声明式 ADD COLUMN + 把旧行 `active=NULL` 修补为 `1` |
| 13–15 | （中间预留/小调整；详见 `hermes_state.py::_init_schema`） |
| 16 | 给 delegate subagent 行打 `_delegate_from` 标记：用 `json_set()` 把 `parent_session_id` 写入 `model_config.$._delegate_from`，避免父会话删除时孤儿行污染 picker |
| 17 | 当前版本 |

---

## 10. 写并发与锁策略

多个 hermes 进程（gateway + CLI sessions + worktree agents）共享同一个 `state.db`。`SessionDB` 的处理：

| 机制 | 值/行为 |
|------|--------|
| SQLite timeout | 1 秒（不使用默认 30s） |
| 应用层重试 | 最多 15 次，jitter 20–150 ms |
| 事务模式 | `BEGIN IMMEDIATE`，在事务开始就暴露锁竞争 |
| WAL checkpoint | 每 50 次成功写后做一次 PASSIVE 模式 checkpoint |

```
_WRITE_MAX_RETRIES = 15
_WRITE_RETRY_MIN_S = 0.020   # 20ms
_WRITE_RETRY_MAX_S = 0.150   # 150ms
_CHECKPOINT_EVERY_N_WRITES = 50
```

避免 SQLite 内部确定性 backoff 导致的"convoy effect"（所有写者按相同间隔重试）。

---

## 11. 常用查询示例

### 会话 lineage（祖先链）

```sql
WITH RECURSIVE lineage AS (
    SELECT * FROM sessions WHERE id = ?
    UNION ALL
    SELECT s.* FROM sessions s
    JOIN lineage l ON s.id = l.parent_session_id
)
SELECT id, title, started_at, parent_session_id FROM lineage;
```

### 最近会话 + 首条 user preview

```sql
SELECT s.*,
    COALESCE(
        (SELECT SUBSTR(m.content, 1, 63)
         FROM messages m
         WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
         ORDER BY m.timestamp, m.id LIMIT 1),
        ''
    ) AS preview,
    COALESCE(
        (SELECT MAX(m2.timestamp) FROM messages m2 WHERE m2.session_id = s.id),
        s.started_at
    ) AS last_active
FROM sessions s
ORDER BY s.started_at DESC
LIMIT 20;
```

### 按 model 统计 token 用量

```sql
SELECT model,
       COUNT(*) AS session_count,
       SUM(input_tokens) AS total_input,
       SUM(output_tokens) AS total_output,
       SUM(estimated_cost_usd) AS total_cost
FROM sessions
WHERE model IS NOT NULL
GROUP BY model
ORDER BY total_cost DESC;
```

### 只读可见消息（带 active 过滤）

```sql
SELECT id, role, content, tool_calls, tool_name, timestamp, finish_reason
FROM messages
WHERE session_id = ?
  AND active = 1
ORDER BY timestamp, id;
```

---

## 12. WebUI 集成位置

| 文件 | 作用 |
|------|------|
| `api/state_sync.py` | 把 WebUI session 的 token/title/model 镜像写入 `state.db`（受 `sync_to_insights` 设置控制，默认关） |
| `api/models.py::_active_state_db_path()` | 解析当前 profile 的 `state.db` 路径（处理 TLS-vs-thread 竞态） |
| `api/goals.py` | 通过 `SessionDB(db_path=home / "state.db")` 读写 goals |
| `api/session_recovery.py` | 用只读 URI `file:...?mode=ro` 读 `state.db`，比对孤儿 session 备份 |
| `api/route_session_list_cache.py` | 监听 `state.db` 文件 stat + WAL + `MAX(rowid)` 指纹，用于缓存失效 |
| `api/routes.py` | `/api/sessions` 合并 `state.db` 行到侧栏；`/api/session/delete` 同步删除 `state.db` 行 |

---

## 13. 维护约束（变更此 DB 时须遵守）

1. **新增普通列**：直接加到 `SCHEMA_SQL` 中即可，`_reconcile_columns()` 会在启动时自动 `ADD COLUMN`。不要为此 bump `SCHEMA_VERSION`。
2. **数据迁移 / 索引变更 / 表结构变更**：必须新增版本块 `if current_version < N:`，并 bump `SCHEMA_VERSION`。
3. **接缝文件修改最小化**：`SCHEMA_SQL` 与 `DEFERRED_INDEX_SQL` 是单一权威来源；不要在 WebUI 仓库里复制 schema 定义，引用本文档即可。
4. **`state.db` 是真实用户状态**：试验时使用隔离的 `HERMES_HOME`，未经授权不得读写真实 `~/.hermes/state.db`。
5. **WAL 模式下的 stat 失效**：文件 mtime 可能在同一 bucket 内不更新，缓存层须额外依赖 `MAX(rowid)` 内容指纹（见 `route_session_list_cache.py`）。
