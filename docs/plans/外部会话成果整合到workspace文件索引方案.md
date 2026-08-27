# 外部会话成果整合到 Workspace 文件索引方案

**状态：提案**  
**范围：** `GET /api/integration/workspace/files` 默认列出已登记、当前仍安全可读的会话外部成果文件；既有删除接口可删除具有精确成果证据的外部文件。  
**不新增请求参数，不新增 HTTP 路径。**

## 1. 背景与问题

当前 `GET /api/integration/workspace/files` 只递归枚举
`HERMES_WEBUI_DEFAULT_WORKSPACE`（下称 *integration root*）之内的普通文件。它适合展示共享
workspace 与 managed session 位于该根目录下的成果，但不会列出以下文件：

- session 通过写入工具、终端或明确交付，在 session workspace 之外生成的文件；
- session 使用显式 external/worktree workspace 且该根不在 integration root 内时生成的成果。

这不应通过扫描每个 session 的任意外部目录来解决。那会把一个受限的 workspace 文件索引扩大为
主机文件浏览器，并绕开现有的 Manifest provenance、敏感路径拒绝和符号链接防护。

项目已经具备较窄的可信通道：Session Manifest 会为具有成功成果证据的外部直接引用持久化一条
Artifact row；单文件接口已经能按“精确已登记 row + 外部路径策略 + 无跟随 fd”预览该文件。本方案
让 `/files` 默认合并这类已登记成果，而不是创建第二套来源判定或授权模型。

## 2. 目标、非目标与术语

### 目标

1. 保持现有 URL、HTTP 方法和请求参数不变；调用同一 `/files` URL 时默认返回 workspace 内文件与
   受信任的外部会话成果的并集。
2. 外部条目与 workspace 条目使用同一套 `q`、`type`、`sort`、`order`、`page`、`page_size` 逻辑，
   因此 `sort=mtime&order=desc` 是跨两类文件的单一排序结果。
3. 已登记且当前仍有效、具有独立 deletion capability 的外部成果可在明确确认后删除；删除不校验当前活动 profile。普通 Artifact 只提供发现和预览，不自动获得删除权。外部文件仍不能保存、重命名、移动、上传或参与 Git 操作，响应不为删除能力新增字段。
4. 复用现有 Manifest 和 `external_references` 的可信来源、路径策略和 race-safe 文件打开，不放宽
   任意绝对路径的访问能力。
5. 保持未传 `profile` 和传入 `profile` 两种查询的现有归属语义；响应继续能够标注成果所属 profile。

### 非目标

- 不枚举任意 session workspace、`/tmp`、用户 home 或调用方传入的目录。
- 不把普通文件读取、搜索或目录列举自动升级为“会话生成的成果”。`media` 以精确持久化 Artifact row 为前提纳入范围，不做路径猜测或目录扫描。
- 不复制、移动、哈希、归档或持久化外部源文件内容；Manifest row 始终是活引用。
- 不新增“全量 session 文件索引”、目录树或外部文件写入、重命名、移动能力。
- 不改变 `GET /api/session/manifest` 与 SSE 的 wire schema；本方案不新增 workspace 文件列表字段，但改变
  `path` 的语义：同一字段现在可能是 integration root 相对路径或已登记外部绝对路径。该破坏性语义变化
  必须在客户端契约测试和迁移说明中明确，不能仅称为结构兼容。

### 术语

| 术语 | 含义 |
| --- | --- |
| workspace entry | integration root 内递归扫描得到的相对路径文件。 |
| 外部 Artifact | 路径位于其 Artifact 根外、且已持久化为 `record_kind=artifact`、`preview=file` 的外部直接引用。 |
| 可列出外部 Artifact | 来源允许、当前路径策略允许，且以无跟随 fd 打开后仍是普通文件的外部 Artifact。 |
| 可删除外部 Artifact | 具有精确 Artifact row 和独立、未撤销 deletion capability，且删除时再次通过外部路径策略与无跟随普通文件验证的文件。Artifact row 的 profile 不参与删除授权。 |
| 混合索引 | workspace entry 与可列出外部 Artifact 合并后，再统一过滤、排序、分页的列表。 |

## 3. 对外契约

### 请求

接口保持不变：

```text
GET /api/integration/workspace/files?order=desc&path=.&page=1&page_size=20&sort=mtime
```

原有参数含义不变：

- `path` 默认 `.`；仅用于 integration root 内的子树筛选。
- `q` 按 basename 不区分大小写匹配。
- `type` 按扩展名匹配。
- `sort` 支持 `path`、`size`、`mtime`、`ctime`。
- `order` 支持 `asc`、`desc`。
- `profile` 继续按 Manifest profile 过滤成果。
- `refresh=1` 继续只强制刷新 workspace 物理扫描缓存。

### 默认合并规则

当 `path=.` 时，响应 `files` 为：

```text
workspace 扫描结果
  + 当前可列出的外部 Artifact
  -> q/type 过滤
  -> sort/order 全局排序
  -> page/page_size 分页
```

当 `path` 为非根子树时，维持现有语义：只返回该 workspace 子树内文件，不合并外部绝对路径。外部路径
不属于 integration root 的任一子树，静默混入会让 `path` 的边界不再可预测。

`profile` 规则如下：

| 查询形态 | workspace 内结果 | 外部 Artifact 结果 |
| --- | --- | --- |
| 未传 `profile` | 全部文件；已有 Manifest 成果可附加 profile | 全部可列出外部 Artifact，附加其 profile（非空时） |
| 传 `profile=ops` | 保持现有“仅 `ops` 的 Manifest 成果文件”语义 | 仅 `ops` 的可列出外部 Artifact |

外部同一规范化绝对路径若有多条符合条件的持久化 row，以 `updated_at` 最新的 row 作为列表归属和
`profile` 来源；列表中只出现一次。相对路径与绝对路径属于不同命名空间，不参与互相去重。

### 响应兼容性

响应对象结构**不新增字段**。外部条目复用既有 `IntegrationWorkspaceFileEntry`：

```json
{
  "path": "/tmp/monthly-report.xlsx",
  "size": 2048,
  "mtime_ns": 1787460613000000000,
  "ctime_ns": 1787460613000000000,
  "ext": ".xlsx",
  "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "profile": "ops"
}
```

- workspace 内条目的 `path` 继续是 integration root 相对路径；外部 Artifact 的 `path` 是已登记的规范化
  绝对路径。这是列表中唯一的来源区分，不增加 `source`、`read_only` 或 session 元数据。
- `workspace` 顶层字段仍为 integration root，不因单个绝对条目改变。
- `total`、`has_more`、`page` 均基于混合索引，而不是分别基于两个来源。
- 路径是否可删除不由响应或浏览器决定。服务端只对精确 Artifact row 关联的、状态为 `active` 的 deletion capability 开放绝对路径删除，不校验当前活动 profile；其他 mutation 路由继续只接受 integration root 内的相对路径。

### 3.1 审查后的强制安全模型

本方案必须区分“文件被会话引用/展示”和“WebUI 被授权删除该文件”：

1. `session_manifest_records` 是派生索引，不是不可篡改的权限审计日志。存在 Artifact row 只能支持列表、预览和历史溯源，不能单独授权 `unlink`。
2. 删除权由独立的 deletion capability 表或等价持久化记录承载，至少包含：`capability_id`、规范化路径、来源资源/事件 ID、文件身份（`st_dev`、`st_ino`、`ctime_ns`，可选内容哈希）、状态（`active`、`deleting`、`deleted`、`revoked`）、创建和撤销时间。
3. capability 必须在受控创建、导入或媒体资源落盘成功时原子签发，并绑定真实创建/资源回执。`assistant_prose`、文本解析的 `MEDIA:`、普通读取/搜索、普通 `edit_file`/`patch`，以及无法证明新建或受控托管的泛化 `terminal` 输出，只能列出或预览，不能签发删除权。
4. `media` 可以删除，但仅限媒体管道提供不可伪造的资源 ID/创建回执并签发 capability 的记录。历史 `source_tool="media"` row 若没有 capability，只能预览，不能通过通用绝对路径接口删除；聊天附件若要删除，应优先走附件资源 ID 的专用删除流程。
5. 列表、预览、删除必须复用同一个 capability 选择函数。不能出现“列表取最新 row、删除任意历史 row 即可”的不一致；同一路径只允许一个确定性的当前 capability（按状态、版本、`updated_at`、`id` 稳定排序）。
6. 删除成功必须原子撤销 capability；Manifest row 可以保留作为历史记录，但不能继续授权。之后同一路径被重新创建时，必须签发新的 capability，旧 row/capability 不得复活删除权。
7. 当前产品明确 profile 不参与删除授权，因此必须在契约中声明：profile 只是展示和过滤字段，不是安全隔离边界；列表、预览、删除对同一认证主体共享所有已授权 capability。若未来 profile 代表不同用户或租户，必须改为校验主体 ACL，不能继续忽略 profile。

## 4. 成果资格与安全边界

外部条目必须同时满足以下条件：

1. 存在持久化 Session Manifest row，且 `record_kind="artifact"`、`preview="file"`。
2. `path` 是规范化后的绝对路径。
3. `source_tool` 符合新增的 `is_workspace_external_artifact_reference()`：成功的文件 mutation、`terminal`、
   `assistant_prose` 或 `media` 成果证据。该 helper 只决定是否可列出/预览；删除还必须通过独立的
   `deletion_capability` 校验。该 helper 可复用既有 `is_external_artifact_reference()`，并额外接受已有
   预览语义中的 `source_tool="media"`；不要改变旧 helper 的既有调用语义。
4. `external_references.policy.open_external_regular_file()` 能从 `/` 开始逐组件以 `O_NOFOLLOW`
   打开，并确认目标仍为普通文件。
5. 路径不命中现有敏感 basename、Hermes state 子目录、系统拒绝根、`.git`/依赖/缓存目录等策略。

明确排除：

- 未登记的绝对路径，即使调用方已知其路径；
- 目录、FIFO、socket、设备文件、损坏/不存在文件与任何 symlink；
- 从成功读取、搜索、`ls`、目录扫描、stdout 猜测出的路径。

`media` 已纳入本方案的可列出、可预览成果范围；只有同时拥有媒体资源真实创建回执/资源 ID 和 active
deletion capability 时才可删除。当前 Manifest row 不含独立的“聊天附件”类别；附件若只有
`source_tool="media"` row 而没有 capability，只能预览，不能通过此接口删除。若要删除附件，应使用附件
资源 ID 的专用删除流程，或先补充可审计的资源归属字段，不能按目录名或文件扩展名猜测。

列表操作只读取当前元数据，随即关闭 fd。列表只标注服务端根据 active capability 得出的可删除状态，不能
把 profile 或前端按钮当作授权依据。预览操作仍由现有
`GET /api/integration/workspace/file?path=<absolute>` 重新查找精确 row 并重新安全打开文件；列表与
预览之间文件被替换、删除或变为 symlink 时，预览必须失败关闭，不能复用列表阶段的路径名或授权。

删除的语义是“删除用户确认时与 active capability 文件身份匹配的当前安全普通文件”，而不是删除某个历史字节快照。删除完成后原子撤销 capability；Manifest row 可以保留，让它在下次投影时成为 `expired` 历史成果，但保留的 row 不再提供删除授权。若路径被重新创建，必须重新签发 capability。确认对话框必须显示 basename 与完整截断路径，避免把删除理解为撤销 Manifest 记录。

## 5. 后端设计

### 5.1 新增外部 Artifact 列表 helper

新增 `integration/session_manifest/external_references/listing.py`，使外部路径 SQL、来源判定和安全
文件打开仍集中在 `external_references/`，而不是复制到 workspace handler 或通用 store。

建议公开一个窄 helper：

```python
def list_registered_external_artifact_entries(
    *, profile: str | None = None
) -> list[dict]:
    """Return only current, safely-openable, read-only external Artifact entries."""
```

职责：

1. 只读查询 `session_manifest_records` 中 Artifact 文件记录和 active deletion capability，按规范化绝对 `path` 收敛至唯一当前记录；没有 capability 的 Artifact 仍可列出/预览，但标记为不可删除（不新增响应字段，服务端内部保留状态）。
2. 应用 `is_workspace_external_artifact_reference()` 来源资格；`media` 与文件生成/变更成果可保留，reference-only 来源在 SQL 结果之后直接忽略。删除资格另由 capability 状态、文件身份和来源回执决定。
3. 对每个候选调用 `open_external_regular_file()`；由返回 fd 的 `fstat` 生成 `size`、`mtime_ns`、
   `ctime_ns`，并通过现有 `MIME_MAP` 计算 `ext` 与 `mime`。
4. 始终关闭 fd；任何 DB、路径、权限或 stat 异常都只跳过该行并记录不含完整外部路径的 debug 日志。
5. 返回与既有 workspace entry 相同的字段形状，并带可选 `profile`；`path` 保持规范化绝对路径，
   不添加来源、只读或 session 元数据。

该 helper 不能返回原始 `workspace_root`、session ID、lineage、turn key、工具参数或目录目标；列表只需要
文件展示与预览所需的最小元数据。

### 5.2 合并位置

修改 `integration/workspace/handlers.py` 的 `_handle_files_list()`：

1. 维持当前 integration root 解析、`page/page_size`、`q/type/sort/order` 校验和 workspace 缓存读取。
2. 在 `path == "."` 时调用外部 Artifact helper；非根子树不调用。
3. 在调用 `paginate_workspace_file_entries()` 前把两类条目合并；不可在 workspace 结果分页后再 append
   外部结果。
4. `profile` 查询时，workspace 分支与外部分支都使用同一个 profile filter。
5. 现有 `artifact_index` 只继续给 integration root 内 Artifact 添加 profile；外部 helper 自己提供 profile，
   不把绝对路径塞进 `get_artifact_profile_index(root)` 的相对路径索引。

推荐抽取一个纯函数，例如 `merge_workspace_file_entries(workspace_entries, external_entries)`，仅负责保持
字典字段形状和路径命名空间，不负责权限、排序或分页。这样调用链是唯一的：

```text
Manifest durable rows
  -> external_references.listing（来源 + fd 安全检查）
  -> workspace handler 合并
  -> paginate_workspace_file_entries（过滤、排序、分页）
  -> JSON response
```

### 5.3 数据库与性能

外部候选的权威来源是 `session_manifest.db`，不是 workspace 进程内缓存。外部文件在 WebUI 不可感知的
位置被移动、改权限或替换，因此本方案**不缓存其可读性或 stat 元数据**。

实现前应先用真实/匿名化开发数据测量候选量。若查询计划证明需要索引，在 Manifest store 的 schema migration
中增加仅服务此只读查询的复合索引，例如覆盖 `record_kind`、`preview`、`path`、`updated_at` 和
`profile`；索引设计须以 `EXPLAIN QUERY PLAN` 为依据，不凭猜测添加。

workspace 扫描缓存仍按当前 root/subtree key 工作：

- `refresh=1` 仅失效 workspace 缓存；外部列表本来每次重新验证，不需要额外刷新语义。
- `Session.save()` 对 managed workspace 的现有根索引失效钩子保持不变。
- Manifest 写入、外部源文件删除和权限变化不会产生 workspace cache 失效要求。

### 5.4 外部 Artifact 删除

保留既有接口与 body 形状：

```text
POST /api/integration/workspace/file/delete
{"paths":["reports/old.md", "/tmp/monthly-report.xlsx"]}
```

相对路径保持当前 integration root 删除逻辑。绝对路径新增受限分支，不能复用 `resolve_integration_rel()` 的相对根解析：

1. 不读取、不校验或推断当前活动 profile；请求 body、列表条目与 Cookie 中的 profile 都不参与删除授权。profile 只是展示/过滤字段，不是安全边界。
2. 按规范化绝对路径选择唯一当前 Artifact/capability 组合；必须存在 `record_kind=artifact`、`preview=file` 的精确 row、`deletion_capability.status=active`、允许的来源回执和匹配的文件身份。普通读取、搜索、纯 `assistant_prose`/文本 `MEDIA:` 和未登记路径不可删除；不存在 capability 的历史 media 只能预览。
3. 在 `integration/session_manifest/external_references/` 新增 `unlink_registered_external_artifact(path)`，集中执行唯一 capability 选择、状态抢占、来源判断、路径 policy、文件身份和删除操作；workspace handler 和 `integration/workspace/delete.py` 只做薄分发。
4. 通过 SQLite 事务或等价 per-path 锁把 capability 从 `active` 原子抢占为 `deleting`，防止并发删除和 Manifest 清理同时获权。失败恢复为 `active`；成功进入 `deleted` 并记录 `deleted_at`。列表与预览排除 `deleting/deleted/revoked` capability。
5. helper 从 `/` 起逐组件以 `O_NOFOLLOW` 打开父目录，最终以 `O_NOFOLLOW` 打开叶文件、`fstat` 确认普通文件并比对 capability 身份，再在同一个父目录 fd 上执行 `os.unlink(leaf, dir_fd=parent_fd)`。禁止先 `Path.resolve()`/`exists()` 再按 pathname unlink，也禁止跟随最终 symlink。对非服务独占、父目录可被其他主体写入的目录，必须拒绝或明确记录为 best-effort，不得宣称完全消除最后一步 rename-swap 竞争。
6. 任一 DB、capability 状态、来源回执、文件身份、路径策略、目录、特殊文件、权限或 race 检查失败时，该 path 单项失败；批量请求中其他独立安全 path 可继续处理。外部绝对路径失败统一返回同一中文错误，不回显登记状态、profile 或受保护路径细节。
7. 成功删除不删除 Manifest row，但必须撤销 capability，不触发 workspace 文件缓存失效；前端从本地列表移除后重新加载，下一次 Manifest 投影将该成果显示为 `expired`。同路径重新创建必须产生新的 capability。

由于 POSIX 没有“按已验证 file descriptor unlink”的可移植原语，本方案的授权对象是删除瞬间的安全目录项，而不是历史 inode 快照。父目录链始终无跟随打开，叶项在 unlink 前立即做无跟随普通文件检查；不能满足该条件的平台或异常路径必须拒绝外部删除。该语义与预览的“当前安全版本”模型一致，确认对话框必须如实说明，不得宣称可删除历史版本或保证内容快照身份。

#### 删除决策表

| 请求中的 `path` | 是否允许删除 | 必须满足的条件 | 处理结果 |
| --- | --- | --- | --- |
| integration root 内的相对普通文件，例如 `sessions/a/report.md` | 允许（既有能力） | 通过现有 `resolve_integration_rel()`、不是目录/cruft，且 anchored unlink 成功 | 删除物理文件；失效 workspace 索引。 |
| 同时具有 active deletion capability 的已登记外部 Artifact 绝对路径（包括 row 属于其他 profile 或 profile 为空） | 允许（本方案新增） | 唯一当前 Artifact/capability 组合；`record_kind=artifact`、`preview=file`；capability 来源回执允许；`media` 必须有资源 ID/创建回执；文件身份匹配；外部策略允许；用户完成确认 | 撤销 capability 后删除物理文件；保留 Manifest row，后续显示 `expired`。 |
| 未登记的任意绝对路径 | 拒绝 | 缺少精确持久化 Artifact row | 单项失败；不能借删除接口探测主机文件。 |
| 仅由普通读取、搜索或目录发现的路径，或没有 deletion capability 的历史 Artifact | 拒绝 | `read_file`、`open_file`、`view_file`、`mcp_filesystem_read_file`、`glob`、`rg`、`grep`、`search`、`semantic_search`、`mcp_filesystem_search_files` 在当前正常采集中均为 reference-only；Artifact row 本身不提供删除权 | 单项失败；如有有效 Artifact row 仍可按只读规则预览。 |
| 指向目录、FIFO、socket、设备或 symlink 的外部路径 | 拒绝 | 不是当前安全普通文件，或无跟随打开失败 | 单项失败；绝不递归删除或跟随 symlink。 |
| 命中 `.env`、Hermes state、系统拒绝根、`.git`/依赖/缓存目录的路径 | 拒绝 | 外部路径策略拒绝 | 单项失败，不回显受保护路径细节。 |
| 已删除、权限变化或在确认后被替换的外部路径 | 拒绝 | 删除时的再次安全验证失败 | 单项失败；不把列表阶段结果当作删除授权。 |

批量 `paths` 按上述规则逐项独立处理：允许的条目可以成功删除，拒绝或失败的条目出现在既有 `failed[]` 中；
不存在“因一个外部 path 失败而回滚其他已成功删除条目”的事务语义。前端的批量删除仅收集 integration root
相对路径；外部 Artifact 始终逐个确认和删除，避免一次确认跨越多个绝对路径。

首次实现的性能验收基线：在 1,000 条已登记外部候选、5,000 条 workspace 文件下，记录
`HERMES_DEBUG_TIMING=1` 的 collect/sort 指标；若安全 stat 成为瓶颈，后续单独设计带生命周期证明的
外部候选缓存，不能在本变更中以未验证缓存换取权限正确性。

## 6. 前端与预览行为

修改 `integration/assets/hermes_integration_workspace.js`，不改变列表请求 URL 或追加参数。

1. 保持当前列表渲染、筛选、排序与加载更多流程；后端返回混合页，前端不得再对当前页二次排序。
2. 将 `path` 判定为绝对路径（同时覆盖 POSIX `/...` 与 Windows 驱动器路径）时，按外部成果展示；可先
   使用现有 profile/meta 行，不新增高频控制。该判定只用于 UI 呈现，绝不是授权依据。具体文案进入
   `en` locale，由已有回退机制处理。
3. 由于响应不新增 capability 字段，前端不能在列表页准确区分“可预览但不可删除”的历史 Artifact；绝对路径条目可显示统一删除入口，点击后展示明确的不可逆确认对话框（含完整截断路径），仍把 `paths:[path]` 交给既有删除接口。服务端以 Artifact + active capability + 文件身份作为最终边界；无 capability 时返回统一失败，前端不得把按钮显示当作授权结果。
4. 外部条目不提供保存、重命名、移动、上传或 Git 操作；这些操作继续只面向 workspace 相对路径。
5. 点击仍调用现有 `API.fileUrl(path)`。该 URL 对绝对路径已走注册 Artifact 的安全预览分支，前端不得
   改为 `file://`、新窗口直接绝对路径或自行构造 token。
6. 文件预览组件保持现有格式分流；外部条目与 workspace 内同类型文件具有同样的文本、图片、PDF、媒体与
   HTML sandbox 展示行为。

响应式验收覆盖桌面、窄屏与移动端：外部绝对路径必须在不撑破行宽的情况下显示 basename 和截断路径，且
外部成果及其删除确认状态清晰可辨。

## 7. 文档与 API 规范

实施时同步更新：

- `integration/swagger/openapi.json`
  - `/api/integration/workspace/files` 说明默认包含安全的已登记外部会话成果；
  - 保持 `IntegrationWorkspaceFileEntry` schema 字段不变；明确 `path` 可为 integration root 相对路径或
    已登记外部 Artifact 的绝对路径；
  - `/api/integration/workspace/file/delete` 说明绝对路径仅限精确 Artifact + active deletion capability 的外部成果，并且需要不可逆确认；profile 不参与删除授权，但文档必须明确 profile 不是安全隔离边界。
- `integration/README.md`
  - 修改 Workspace 无会话文件段落，说明默认混合索引与外部列表安全边界；
  - 保持示例 URL 不变，新增一个能展示绝对 path 的响应示例。
- `docs/api/session-manifest-api.md` 与 `docs/architecture/session-manifest-artifacts.md`
  - 将“绝对路径不会出现在 `/files` 枚举结果中”的旧边界替换为本方案的精确、已登记例外，并记录受限删除语义；
  - 保持 Manifest 本身不是 workspace 文件清单的定位不变。
- `integration/CHANGELOG.md`
  - 实现完成时记录 Fork 集成层的用户可见行为；不修改根目录 `CHANGELOG.md`。

## 8. 测试计划

所有 pytest 通过 `./scripts/test.sh` 执行。新测试优先放在 `integration/tests/workspace/` 与
`integration/tests/session_manifest/`。

### Handler 与列表语义

1. 默认根查询混合 workspace 相对文件与两个外部 Artifact，按 `mtime desc` 后分页；证明 page 1/2
   没有重复和漏项。
2. `q`、`type`、`size`、`mtime`、`ctime` 的排序与过滤跨两种来源生效。
3. `path=subdir` 不包含外部绝对条目。
4. `profile` 查询只保留该 profile 的 workspace Artifact 和外部 Artifact；无 profile 查询带 profile 标注。
5. 同一外部绝对路径被多个 row 引用时，只有最新 row 的 profile/元数据出现一次。
6. 空结果、越界页和现有非法 query 参数响应保持兼容。

### 安全与生命周期

1. 未登记绝对文件绝不出现在列表，也不能经单文件预览读取。
2. `.env`、Hermes state、`~/.ssh`、系统拒绝根、忽略目录下的已登记路径仍不列出。
3. 目标删除、改为目录、FIFO 或 symlink 后，从列表消失；预览请求返回失败而不泄露路径细节。
4. 列表获得结果后再替换为 symlink，预览重新验证并拒绝，证明不存在 check-then-use 漏洞。
5. `source_tool=media` 可进入外部成果列表；只读读取、目录列举和纯助手猜测路径不进入。只有 media 资源回执对应的 capability 才可删除。
6. 外部删除只接受精确 Artifact + active capability；profile 不参与授权。没有 capability 的 media、附件、未登记路径、reference-only 来源与受保护路径均失败关闭。
7. 删除前后替换 symlink、目录、FIFO 或普通文件时不跟随链接；文件身份不匹配则拒绝。成功后 capability 撤销，Manifest row 保留并投影为 expired。
8. 相同绝对路径存在 allowed 与 disallowed 历史 row 时，列表、预览和删除都使用同一 canonical capability；不能由隐藏旧 row 绕过列表资格。
9. capability 被并发删除、会话清理或 turn replace 时，只有一个请求能从 `active` 抢占为 `deleting`；其他请求失败关闭，不能重复 unlink。

### 前端

1. 默认请求不包含新 query 参数，且后端混合响应可直接渲染。
2. 外部条目可进入现有预览路径；workspace 条目行为不变。
3. 由于响应不新增 capability 字段，绝对路径条目显示统一删除入口并在确认后由服务端判定；其他 profile 或空 profile 不影响删除资格，无 capability 的 Artifact/media 必须被服务端拒绝。
4. 删除确认对话框展示路径；成功后列表移除，Manifest 历史记录在刷新后为 expired。
5. 桌面、窄屏、移动端截图覆盖长绝对路径、无 profile 和有 profile 三种视觉状态。

### 回归集

至少运行：

```bash
./scripts/test.sh integration/tests/workspace/ integration/tests/session_manifest/ tests/test_session_manifest_contract.py
```

并补充受影响的静态 UI 和路由接缝测试；PR 中报告实际执行命令与无法验证的项。

## 9. 分阶段实施

### 阶段 0：基线与契约确认

1. 记录生产/测试环境的外部 Artifact row 数量和 `EXPLAIN QUERY PLAN`，不读取文件内容。
2. 明确产品上“会话生成成果”包含 `media`；但只有媒体资源真实创建回执签发的 capability 可删除，历史 `source_tool="media"` row 没有 capability 时只能预览。附件优先走资源 ID 删除。
3. 更新本方案状态和实施 issue 的 Contract Routing，确认列表响应字段保持兼容、删除语义是既有接口的受限绝对路径扩展。

完成条件：确认候选规模与 SQL 查询形状，且没有把任意 session workspace 扫描纳入范围。

### 阶段 1：安全列表数据源

1. 在 `integration/session_manifest/external_references/` 新增 list helper 与单元测试。
2. 复用现有 policy 的无跟随 fd 打开；禁止以 `Path.stat()` + 后续 pathname 打开替代。
3. 先证明新增测试在旧代码上失败，再接通 helper。

完成条件：helper 只能输出当前可安全读取、已登记的外部成果，且每个 fd 在每条成功/失败路径关闭。

### 阶段 2：混合索引与契约更新

1. 在 workspace handler 根查询分支合并两类 entry，再统一排序分页。
2. 更新 Swagger、Integration README 与 Manifest 相关契约文档。
3. 增加混合分页、profile 与失效文件回归测试。

完成条件：原 URL 默认展示外部成果；非根 `path` 和所有原 query 错误行为不变。

### 阶段 3：受控删除与前端投影

1. 完成外部 Artifact 删除 helper 与 profile 无关的登记资格测试。
2. 完成 deletion capability 的签发、抢占、撤销、路径复用和文件身份校验；没有 capability 的历史 Artifact/media 只能预览。
3. 为具备 active capability 的绝对 `path` 提供明确确认后的删除入口；保留其他外部 mutation 的禁用状态。
4. 显示低干扰的外部成果标记与安全截断路径。
5. 做三种视口截图和真实点击验证。

完成条件：用户只能删除精确已登记且具备 active deletion capability 的外部成果；profile 不影响删除资格，任何其他绝对路径、无 capability 的 Artifact、外部 mutation 或无确认路径均被拒绝。

## 10. 风险、回滚与验收不变量

| 风险 | 处理与不变量 |
| --- | --- |
| 外部路径泄露或越权读取 | 列表与预览都要求精确持久化 row；列表不回显被拒绝候选；预览重新进行无跟随 fd 校验；同一认证主体共享 profile，不把 profile 当 ACL。 |
| 外部文件被误删 | 删除要求精确 Artifact、active capability、来源回执、文件身份、确认对话框和无跟随 fd 删除；成功后撤销 capability；其他绝对 path 拒绝。 |
| 分页不稳定 | 合并后一次排序再分页；不允许分别分页后拼接。文件实时变化沿用现有无快照分页语义。 |
| workspace 缓存陈旧 | 外部来源不进入 workspace 缓存；workspace 缓存失效机制不变。 |
| Manifest 被误当成文件清单 | 仅消费其已验证 Artifact row；不做 backfill、目录扫描、相似路径推断或 transcript 扫描。 |
| 性能退化 | 先测量候选数与 query plan；SQL 不能消除每个候选的安全 open/fstat。上线前必须设候选数量、单次耗时和降级上限，避免无限增长的 Manifest DB 造成 DoS；缓存设计另行评审。 |

回滚是低风险的：停止在 handler 中合并外部 helper 并拒绝绝对路径删除后，原 URL 立即恢复为 workspace-only 列表；Manifest row 与既有单文件预览能力均不迁移、不删除、不受影响。已被用户确认并成功删除的外部源文件不可由此回滚恢复。

最终验收必须证明以下不变量：

1. 默认列表能找到会话生成、但位于 integration root 外的已登记成果。
2. 知道一个任意绝对路径不足以让它出现在列表、被预览或被删除。
3. 同一条目的排序、过滤、计数和翻页与 workspace 文件处于同一集合。
4. 任何 profile 的精确已登记且具备 active deletion capability 的外部成果都能在确认后删除；保存、重命名、移动、上传和 Git mutation 继续 fail closed。
5. 只有 active deletion capability 对应的文件能在确认后删除；文件删除、权限变化、symlink/普通文件替换、Manifest row 缺失或 capability 撤销都 fail closed。
6. 删除成功后旧路径不能因文件重建、旧 Manifest row 或隐藏历史 row 重新获得删除权。
