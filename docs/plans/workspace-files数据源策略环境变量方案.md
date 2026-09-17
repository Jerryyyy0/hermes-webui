# Workspace Files 数据源策略环境变量方案

**状态：** Proposed（仅设计，尚未实现）
**范围：** `HERMES_INTEGRATION=1` 时的 `GET /api/integration/workspace/files` 文件候选枚举。
**不在范围：** 单文件读取、覆盖、删除、Session Manifest GET、右侧会话 Workspace、历史 Artifact 自动修复。

**关联契约与实现：**

- [`../../integration/workspace/handlers.py`](../../integration/workspace/handlers.py)
- [`../../integration/workspace/file_index_cache.py`](../../integration/workspace/file_index_cache.py)
- [`../../integration/session_manifest/store.py`](../../integration/session_manifest/store.py)
- [`../../api/workspace.py`](../../api/workspace.py)
- [`../api/session-manifest-api.md`](../api/session-manifest-api.md)
- [`../architecture/session-manifest-artifacts.md`](../architecture/session-manifest-artifacts.md)
- [`../../integration/swagger/openapi.json`](../../integration/swagger/openapi.json)

## 1. 结论

新增进程级环境变量：

```bash
HERMES_INTEGRATION_WORKSPACE_FILES_SOURCE=filesystem
HERMES_INTEGRATION_WORKSPACE_FILES_SOURCE=manifest_db
```

默认值为 `filesystem`，保持现有部署升级后的行为不变。

两种策略只决定“从哪里得到候选路径”，不改变最终文件有效性判断：

```text
filesystem
  -> 递归扫描 HERMES_WEBUI_DEFAULT_WORKSPACE
  -> 得到 workspace 当前全部普通文件候选

manifest_db
  -> 查询 session_manifest.db/session_manifest_records
  -> 得到 Manifest 已确认的 file Artifact 候选

两者共同
  -> workspace 路径投影与子树过滤
  -> 安全解析
  -> 当前文件存在性、普通文件与 cruft 校验
  -> stat 补齐 size/ext/mime/mtime_ns/ctime_ns
  -> q/type 过滤、排序、分页
  -> HTTP 响应
```

`manifest_db` 不是磁盘文件索引的等价实现。它有意把接口语义缩窄为“当前仍存在的、具有会话成果证据的 workspace 文件”。手工创建、外部程序创建、只被读取但未成为 Artifact 的文件不会出现。

## 2. 为什么不能把 DB row 直接原样返回

`session_manifest_records` 是 Artifact decision store，不是 workspace mirror。现有 row 只保存：

```text
session_id
lineage_key
profile
workspace_root
turn_key
record_kind
path
preview
source_tool
created_at
updated_at
```

它不保存当前文件的 `size`、`mime`、`mtime_ns`、`ctime_ns`，也不保证源文件此刻仍然存在。磁盘文件还可能在入库后被删除、替换为目录或 symlink，或者因安全策略变化而不再允许访问。

因此 `manifest_db` 模式只能用 DB 代替递归枚举，不能取消逐候选的文件系统验证。响应中的文件元数据必须继续来自本次请求时的 `stat`，不能用 Artifact row 的 `updated_at` 冒充文件修改时间。

## 3. 配置契约

### 3.1 环境变量

| 名称 | 合法值 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `HERMES_INTEGRATION_WORKSPACE_FILES_SOURCE` | `filesystem` / `manifest_db` | `filesystem` | 选择 `/api/integration/workspace/files` 的候选路径来源 |

解析规则：

1. 对值执行 `strip().lower()`。
2. 未设置或空字符串解析为 `filesystem`。
3. 只接受两个完整字面值，不接受 `fs`、`db`、`manifest` 等别名，避免长期兼容多个拼写。
4. 环境变量为进程级配置，修改后重启 WebUI 生效；不提供请求参数覆盖，避免同一部署被调用方切换为更宽的枚举范围。
5. 非法值不得静默回退到 `filesystem`。回退会在运维误配时意外扩大可见文件集合；接口应返回 `500`，错误文案使用中文，并记录不含敏感数据的配置错误日志。

建议在 `integration/config.py` 中集中定义：

```python
WORKSPACE_FILES_SOURCE_FILESYSTEM = "filesystem"
WORKSPACE_FILES_SOURCE_MANIFEST_DB = "manifest_db"


class WorkspaceFilesSourceConfigError(ValueError):
    pass


def workspace_files_source() -> str:
    ...
```

Handler 必须先解析一次策略，并把同一个已解析值用于本次请求的选择、执行和响应，不能在不同阶段重复读取环境变量。

### 3.2 响应可观测性

在现有响应顶层新增字段：

```json
{
  "workspace": "/workspace",
  "source": "manifest_db",
  "files": []
}
```

`source` 固定为本次请求实际使用的 `filesystem` 或 `manifest_db`。这是向后兼容的新增字段，现有前端可忽略；运维和调用方可以据此确认部署配置是否生效。

不增加 query 参数 `source`。策略属于部署侧的数据暴露边界，不应由浏览器任意切换。

## 4. 两种策略的精确语义

### 4.1 `filesystem`

无 `profile` 时保持当前逻辑：

1. 根目录固定为 `resolve_trusted_workspace(None)`，即启动时的 `HERMES_WEBUI_DEFAULT_WORKSPACE`。
2. 使用 `get_workspace_file_entries(root, rel, force_refresh=...)`。
3. 递归过程继续排除 `.git`、`node_modules`、`__pycache__` 等目录，以及 `.DS_Store`、`Thumbs.db`、`._*` 等 cruft。
4. 使用 Manifest DB 的 profile index 给已登记成果附加可选 `profile`，但 DB 不参与决定普通文件是否进入列表。
5. `refresh=1` 清理对应 workspace/subtree 的进程内缓存并重新扫描。

带 `profile` 时继续使用 Manifest DB 取得该 Profile 的 Artifact 路径，再只对候选路径做 `stat`。这是因为文件系统本身没有 Profile provenance；即使策略为 `filesystem`，也不能通过目录扫描可靠推断文件属于哪个 Profile。

### 4.2 `manifest_db`

无 `profile` 时：

1. 查询所有 `record_kind='artifact' AND preview='file' AND path<>''` 的 row。
2. 将 legacy `workspace_root=''` 按现有契约解释为启动时的默认 workspace。
3. 仅保留 `workspace_root` 等于 integration root 或位于其下的 row。
4. 将 session root 相对路径前缀投影到 integration root。例如：

   ```text
   integration root: /workspace
   row workspace_root: /workspace/sessions/abc
   row path: exports/report.md
   API path: sessions/abc/exports/report.md
   ```

5. 排除位于 integration root 之外的外部绝对 Artifact。它们仍可通过 Session Manifest 和既有单文件预览授权访问，但不得混入 workspace 枚举。
6. 按投影后的 API path 去重，并对每个候选执行当前文件校验和 `stat`。

带 `profile` 时，在上述条件基础上增加 `profile = ?` 精确、区分大小写过滤。同一真实文件若有多个 Profile 的 Artifact 证据，在各自 Profile 过滤结果中都可出现一次。

`manifest_db` 无 `profile` 时也必须包含 `profile=''` 的有效 Artifact，只是不在条目上输出 `profile`。不能复用当前会丢弃空 Profile row 的 `get_artifact_profile_index()` 作为候选全集。

### 4.3 同一路径的 Profile 投影

无 `profile` 查询中，同一投影路径可能有多条 Artifact row。文件只返回一次，Profile 标注规则为：

1. 在该投影路径的全部有效 row 中选择 `updated_at` 最大的 row。
2. 若最新 row 的 `profile` 非空，输出该值。
3. 若最新 row 的 `profile` 为空，不输出 `profile`；不得退回更旧的非空 Profile，否则会把旧归属误报为当前归属。
4. `updated_at` 完全相同时，以较大的 row `id` 作为稳定 tie-breaker。

该规则应由 store helper 一次性返回，不允许 handler 再拼第二套“最新 Profile”逻辑。

## 5. Store 查询设计

在 `integration/session_manifest/store.py` 新增专用只读入口，避免 handler 直接写 SQL：

```python
def list_workspace_artifact_candidates(
    workspace_root: Path | str,
    *,
    profile: str | None = None,
    db_path: Path | str | None = None,
) -> list[dict[str, str]]:
    """返回投影到 integration root 的去重 file Artifact 候选。"""
```

建议返回最小结构：

```json
[
  {"path": "sessions/abc/report.md", "profile": "ops"},
  {"path": "shared/result.csv", "profile": ""}
]
```

必须区分以下两种结果：

- 查询成功且无 row：返回空列表。
- SQLite 打不开、schema/query 失败：抛出专用 `ManifestStoreReadError`。

当前部分 profile helper 在 SQLite 异常时返回空集合；它们可继续服务于 `filesystem` 模式的非权威标注，但 `manifest_db` 模式不能复用这种“异常等于空数据”的错误语义。Handler 捕获 `ManifestStoreReadError` 后返回 `500`：

```json
{"error":"Workspace Files 成果索引读取失败"}
```

不得在 DB 失败时自动改走 `filesystem`。自动降级会改变管理员选择的数据可见范围，并把一次明确故障伪装成成功响应。

### 5.1 索引

现有索引主要服务 session/lineage 查询。实现时新增：

```sql
CREATE INDEX IF NOT EXISTS idx_session_manifest_records_workspace_files
ON session_manifest_records(record_kind, preview, profile, workspace_root, updated_at);
```

workspace root 的父子包含关系仍需在 Python 中使用现有安全投影 helper 判断；不能用未经边界处理的 SQL `LIKE '/workspace%'`，否则 `/workspace-other` 会被误纳入。

先通过真实部署量级记录 `artifact_ms`；如果全局 Artifact row 数仍很大，再考虑维护独立投影表。第一期不新增第二份持久化索引状态。

## 6. Handler 改造

在 `integration/workspace/handlers.py::_handle_files_list()` 中只保留一个策略分支，后续过滤/排序/响应共用：

```text
解析并固定 source
  |
  +-- filesystem + 无 profile
  |     -> cached recursive entries
  |
  +-- filesystem + profile
  |     -> DB profile paths -> stat entries
  |
  +-- manifest_db
        -> DB artifact candidates(profile optional) -> stat entries

entries
  -> q/type filter
  -> sort/order
  -> page/page_size
  -> response(source=resolved source)
```

建议把分支封装为 integration workspace 内部 helper，例如：

```python
def collect_workspace_file_entries(
    root: Path,
    rel: str,
    *,
    source: str,
    profile: str | None,
    force_refresh: bool,
) -> list[dict]:
    ...
```

这样 handler 只负责 HTTP 参数、错误映射和 JSON，策略细节不会继续堆积在接缝函数中。

### 6.1 `path` 子树语义

两种策略都必须遵守现有 `path` 参数：

- `path=.`：整个 integration root。
- `path=reports`：只返回 `reports/` 自身及其后代文件。
- `path=report` 不能匹配 `reports/...`。
- `..`、绝对路径、symlink 逃逸继续由 `safe_resolve_ws()` 拒绝。

Manifest 候选在 `stat` 前先按规范化的目录边界过滤，避免读取无关候选。当前 profile 快路径没有把 `rel` 传给显式路径收集函数；实现本方案时应在共享 helper 修正该同类行为，并增加回归测试。

### 6.2 `refresh=1`

| source | 行为 |
| --- | --- |
| `filesystem` | 清理相应文件索引缓存并重新递归扫描 |
| `manifest_db` | 接受参数但不执行 repair/backfill；DB 每次请求本就重新查询，文件元数据也逐候选重新 `stat` |

`refresh=1` 在任何模式下都不能触发 Manifest 历史修复、transcript 扫描或 DB 写入。GET 接口继续保持只读。

### 6.3 计时与日志

保留 `HERMES_DEBUG_TIMING=1` 的三段计时：

- `Artifact-Ms`：DB candidate/profile 查询。
- `Collect-Ms`：递归扫描，或 DB 候选的安全解析与 `stat`。
- `Sort-Ms`：过滤、排序与分页。

响应新增 `source` 后，调试日志也应带该字段。日志不得输出完整 DB 内容或外部绝对 Artifact 路径。

## 7. 生命周期与一致性

### 7.1 文件与 DB 的不同步是可预期状态

| 事件 | `filesystem` | `manifest_db` |
| --- | --- | --- |
| 手工创建文件 | 缓存失效/`refresh=1` 后可见 | 不可见，除非后续形成 Artifact row |
| Agent 成功产出并结算 Artifact | cache invalidation 后可见 | row 持久化且文件仍存在时可见 |
| 文件删除 | cache invalidation/refresh 后消失 | 下一请求 `stat` 失败，立即从列表省略；row 保留供 Manifest 显示 expired |
| 重生成删除 Artifact row、保留磁盘文件 | 文件仍可见 | 文件消失于列表 |
| 历史会话没有 decision | 文件可见 | 不可见；GET 不自动 backfill |
| DB 暂时不可用 | 全量文件列表仍可返回，Profile 标注可缺失 | 返回 500，不降级扫描 |

### 7.2 Cache 所有权

- `filesystem` 的递归索引继续由 `integration/workspace/file_index_cache.py` 拥有。
- `Session.save()`、overwrite、delete 等现有失效入口继续负责清理该缓存。
- `manifest_db` 不新增候选路径内存缓存；SQLite/WAL 是权威来源，每次请求查询。
- 两种模式都不缓存单个文件的 `stat`，避免 DB row 与磁盘状态之间形成新的陈旧窗口。

## 8. API 与文档同步

实现时需同步修改：

1. `.env.example`：增加变量、合法值、默认值及语义差异。
2. `integration/README.md`：说明部署策略、`profile`、`path`、`refresh` 在两种模式下的行为。
3. `integration/swagger/openapi.json`：
   - `/api/integration/workspace/files` 描述不再固定宣称“全部文件”；
   - response schema 增加必填 `source`；
   - 增加配置错误/Manifest DB 读取失败的 `500`；
   - 明确环境变量不是 query 参数。
4. `integration/CHANGELOG.md`：记录 Fork 侧可见行为；不修改根目录 `CHANGELOG.md`。
5. `docs/architecture/session-manifest-artifacts.md`：只补充“Workspace files 可选消费 Artifact row 作为候选源”，不得把 Manifest 改写成 workspace 全量索引。

不需要修改 Session Manifest HTTP/SSE schema。

## 9. 测试计划

### 9.1 配置解析

新增 `integration/tests/workspace/test_source_config.py`：

- 未设置、空值 -> `filesystem`。
- `filesystem`、大小写/首尾空格规范化。
- `manifest_db`、大小写/首尾空格规范化。
- `db`、`manifest`、随机值 -> 明确配置异常。

### 9.2 Store

扩展 `tests/test_session_manifest_store.py`：

- 只返回 `record_kind=artifact AND preview=file AND path<>''`。
- empty decision marker 不进入候选。
- integration root、子 session root 正确投影。
- root 外部 row 与绝对外部 Artifact 不进入候选。
- legacy empty root 映射默认 workspace。
- 同 path 多 turn/lineage 去重。
- 无 profile 查询包含空 Profile row。
- profile 查询精确且区分大小写。
- 最新 row 决定无过滤响应的 Profile；同时间以 id 稳定决胜。
- SQLite 查询异常抛出 `ManifestStoreReadError`，成功空结果仍返回 `[]`。

### 9.3 Handler 可观察行为

扩展 `integration/tests/workspace/test_handlers.py`：

- `filesystem` 无 profile 调用递归缓存，不调用 DB candidate collector。
- `filesystem` + profile 继续使用 DB 路径快路径。
- `manifest_db` 无 profile 不调用 `get_workspace_file_entries()`。
- `manifest_db` 只返回有 row 且当前存在的文件。
- DB row 指向缺失文件、目录、cruft、symlink 或逃逸路径时不返回。
- `path` 在两种模式及 profile 快路径中都限制到精确子树。
- `q`、`type`、四种 sort、两种 order、分页在两种模式结果一致。
- `refresh=1` 在 filesystem 强制重扫，在 manifest_db 不触发 scan、repair 或写库。
- 响应顶层 `source` 与实际分支一致。
- 非法配置返回中文 `500`，且扫描和 DB 查询均未发生。
- Manifest DB 失败返回中文 `500`，不回退 filesystem。
- Manifest DB 成功但无成果返回 `200`、`files=[]`、`total=0`。

### 9.4 邻接回归

```bash
./scripts/test.sh \
  integration/tests/workspace/test_source_config.py \
  integration/tests/workspace/test_handlers.py \
  integration/tests/workspace/test_file_index_cache.py \
  integration/tests/workspace/test_hooks.py \
  tests/test_session_manifest_store.py \
  tests/test_session_manifest_contract.py -q
```

实现前先把新增行为测试写成失败用例，确认失败原因确实是“尚未支持 source 策略”，再实现通过。

## 10. 手动验证矩阵

使用隔离状态与 workspace，不读取真实用户状态：

```bash
HERMES_HOME=/tmp/hermes-webui-agent-home \
HERMES_WEBUI_STATE_DIR=/tmp/hermes-webui-agent-state \
HERMES_WEBUI_DEFAULT_WORKSPACE=/tmp/hermes-webui-workspace \
HERMES_INTEGRATION=1 \
HERMES_INTEGRATION_WORKSPACE_FILES_SOURCE=filesystem \
HERMES_WEBUI_PORT=8789 \
python3 bootstrap.py
```

准备三类文件：普通手工文件、已登记 Artifact、DB row 已存在但磁盘已删除的 Artifact。分别以两种 source 启动后验证：

| 检查 | filesystem | manifest_db |
| --- | --- | --- |
| 手工文件 | 显示 | 不显示 |
| 有效 Artifact | 显示 | 显示 |
| 缺失 Artifact | 不显示 | 不显示 |
| `source` 响应字段 | `filesystem` | `manifest_db` |
| `refresh=1` | 重扫 | 不触发重扫/修复 |
| `profile=ops` | 仅 ops Artifact | 仅 ops Artifact |
| `path=subdir` | 仅子树 | 仅子树 |

还需检查文件预览、覆盖和删除仍按既有路径工作，因为 source 策略不应改变这些端点。

## 11. 实施顺序

1. 在 `integration/config.py` 增加严格的 source 解析和异常类型。
2. 先补 Store/Handler 失败测试，覆盖 DB 错误与空结果的区分。
3. 在 `integration/session_manifest/store.py` 实现候选查询、根目录投影、去重与 Profile 决胜。
4. 在 `integration/workspace/` 内新增共享 collector，统一 source/profile/path/refresh 选择。
5. 将 `_handle_files_list()` 收敛为参数解析、collector 调用、分页和响应。
6. 增加 SQLite 索引并执行 Store、Workspace、Manifest 邻接回归。
7. 更新 `.env.example`、integration README/OpenAPI/CHANGELOG 和 Artifact 架构说明。
8. 在隔离 workspace 下完成两种模式的 curl 验证与性能对比。

## 12. 验收标准

1. 未配置环境变量时，接口行为与当前 `filesystem` 行为一致，仅新增响应 `source` 字段。
2. `manifest_db` 模式不执行递归 workspace 扫描，只处理 Manifest DB 返回的候选路径。
3. `manifest_db` 只返回当前存在、位于 integration root 内且通过既有安全规则的 file Artifact。
4. DB 读取失败不会返回伪造的成功空列表，也不会自动扩大到 filesystem 全量扫描。
5. `path`、`profile`、搜索、类型过滤、排序、分页在两种模式下保持同一 HTTP 契约。
6. GET 不写 Manifest DB，不触发 repair/backfill，不改变 session recency。
7. 外部绝对 Artifact 仍只走 Session Manifest + 单文件预览授权，不进入 workspace files 枚举。
8. OpenAPI、integration README、`.env.example`、Fork changelog 与实现同步。
9. 自动化测试和隔离环境手动验证均通过，并记录两种模式的 `Artifact/Collect/Sort` timing。

## 13. 明确不做

- 不把 `session_manifest.db` 宣称为 workspace 文件系统镜像。
- 不为 `manifest_db` 模式自动扫描 transcript、repair empty decision 或 backfill 历史会话。
- 不把环境策略暴露为可由请求方覆盖的 query 参数。
- 不将外部 workspace、附件目录、memory 或任意绝对 Artifact 合并进平铺文件列表。
- 不修改文件预览、覆盖、删除端点的授权边界。
- 不在第一期建立文件 watcher 或第二张全量 workspace index 表。
