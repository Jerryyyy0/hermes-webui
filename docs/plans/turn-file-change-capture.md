# 当前对话轮次文件变更捕获方案

## 结论

方案可行，但不能只在 WebUI 侧复用工具回调，也不能把现有 `CheckpointManager` 或 Codex `turn/diff/updated` 直接当成“本轮所有文件变更”的完整答案。

可靠实现需要满足一个核心原则：

> 由真正执行文件 I/O、能够访问实际 workspace 的运行时负责建立基线和结算；WebUI 只负责绑定对话 turn、校验执行身份、持久化和展示。

最终架构为：

```text
WebUI 分配 stream_turn_key
  -> 向执行端发送 TurnCaptureRequest
  -> 执行端解析实际 workspace / environment
  -> 执行端建立可恢复的 baseline
  -> Agent / Codex / shell / MCP 执行
  -> 受控写入产生 AppliedFileDelta，并更新内存净变更 tracker（实时投影）
  -> 执行端比较 baseline 与最终状态（完整性兜底）
  -> 生成 TurnFileChangeSet
  -> WebUI 按 session + stream + turn owner guard 持久化
  -> SSE 乐观展示，GET 返回最终权威结果
```

若执行端不支持快照、workspace 不可达或扫描预算耗尽，必须返回 `capture_status=partial|unknown`，不能返回空变更并伪装成功。

## 审查依据与已确认的 V1 决策

本方案基于以下源码与公开产品资料核实：

- Hermes WebUI：`effd307130d9875386244e311746e7b09094fbfa`
- Hermes Agent：`b272fb77e2f136355df6974a932294ee146555f1`
- Codex 官方开源仓库：`openai/codex` 的 `9ded177ce7c1c0bd2047f902936c177612ab3434`。已直接审查 `TurnDiffTracker`、每个用户 turn 的 tracker 创建点、`AppliedPatchDelta` 与 app-server notification schema。
- Codex app-server 当前公开契约：`fileChange.changes[]` 包含 `path/kind/diff`，`turn/diff/updated` 包含本轮最新聚合 unified diff；官方文档明确 app-server 实现开源于同一仓库。
- 腾讯 CodeBuddy WorkBuddy：仅使用其公开文档验证产品语义（按任务呈现文件变更、文件树与产物，并支持并行任务）；客户端源码未公开，本文不把其内部实现当作事实依据。

后续实现前应重新生成当前安装版本的 Codex app-server JSON Schema，因为 app-server schema 与本机 Codex 版本绑定。

本轮设计评审已经确认下列不可变边界：

- V1 只递归捕获实际 execution workspace 根目录；根外写入不读取、不持久化，未来须通过独立授权 scope 加入。
- 同一 workspace 的重叠 capture 可以同时观察到同一外部写入；每个 capture 都保留该变化，并标为 `attribution=ambiguous`，绝不猜测唯一 owner。只有共享 watcher journal 或受控 delta 能证明同一事件时才关联 `change_event_id`；snapshot 只能证明区间净变化时不伪造该 ID。
- 本地 provider 的完整支持目标是 macOS、Linux、原生 Windows 与 WSL；Docker、SSH、Gateway 等环境必须由实际执行端提供 provider，否则诚实返回 `unknown`。
- 普通策略 `observe` 允许任务在无法建立 baseline 时执行但返回 `unknown`；`require_complete` 在 baseline 不能证明完整时拒绝启动。
- terminal 是本轮硬边界。它之后仍在运行的后台进程另走延迟 capture，可逻辑关联原 `turn_key`，但不能反写已结算的结果。
- 变更集是独立执行账本，不并入 Session Manifest 的 Artifacts；前者包含删除、缓存与失败/取消期间写入，后者只表达可交付成果。

## 捕获语义

### “本轮所有文件变更”的定义

本方案捕获的是：

> 从执行端成功建立 baseline，到该执行端报告 turn terminal 状态之间，实际 execution workspace 内可观察到的文件系统净变化。

范围包括：

- 常规文件的新建、删除、内容更新和类型变化；
- 符号链接的新建、删除和目标变化，但不跟随 workspace 外目标；
- 可执行位等受支持的文件 mode 变化；
- `write_file`、`patch`、Codex `apply_patch`、MCP/插件写入；
- shell、`terminal`、`execute_code`（Python 的 `open()`、`pathlib`、`os`、其子进程与 RPC 工具调用）、重定向、heredoc 和构建命令造成的间接写入；
- 成功、失败、部分失败和取消 turn 在结算时已经落盘的净变化。

“净变化”意味着：

- 新建后又删除，最终不出现在 change set；
- 修改后恢复到 baseline 内容，最终不出现在 change set；
- 受控工具的中间操作仍可进入内部 delta journal，但不进入最终净 diff；
- turn 结算后仍在后台运行的进程造成的后续写入不属于该 turn，已知存在后台进程时写入 diagnostics。

本方案不声称仅凭前后快照能证明“哪个进程”造成了变化。若另一个进程同时写入同一 workspace，该变化仍属于捕获区间，但 `attribution=ambiguous`。两个 capture 的观察窗口重叠时，同一变化可以进入两者的结果，并在 diagnostics 互列 `overlap_capture_ids`。仅当共享 watcher journal 或受控 delta 保留了同一事件证据时，两个结果才携带相同 `change_event_id`；snapshot 不能证明这一点时，该字段为 `null`，UI/导出不得把同路径/同 hash 猜成同一写入。

### 唯一默认排除项

为了符合“所有”的目标，不默认排除 `node_modules`、`.venv`、构建输出、缓存、日志、二进制或密钥文件。它们必须参与路径、类型、大小和内容摘要比较。

只排除：

- workspace 内部的 VCS 管理数据，例如 `.git/`、`.hg/`、`.svn/`；
- 本捕获机制自己的 baseline/CAS 临时目录；
- 明确位于 execution workspace 外的路径。

排除项必须随结果返回。预算或权限导致未扫描不是 exclusion，而是 `partial|unknown`。

### 变更完整与内容展示分离

必须区分三个维度：

| 维度 | 值 | 含义 |
| --- | --- | --- |
| `capture_status` | `complete / partial / unknown` | 变更路径和 before/after 指纹是否完整 |
| `attribution` | `controlled / interval / ambiguous` | 是否能归因到受控工具，或只能归因到时间区间 |
| `content_fidelity` | `full / mixed / metadata_only` | 是否保留了足够内容生成文本 diff |

密钥或超大二进制可以被完整检测为“已变化”，同时保持 `content_fidelity=metadata_only`。这不应错误地把 `capture_status` 降为 `partial`。

受控实时 diff 与最终 change set 的可靠性也必须分开：

| 字段 | 含义 | 何时失效 |
| --- | --- | --- |
| `controlled_delta_status` | 受控工具 delta 能否继续生成准确的实时净 diff | 执行端无法确认一次已发生 I/O，或 reconciliation 发现冲突 |
| `capture_status` | 最终 snapshot 是否完整列出结算时的 workspace 净变化 | 扫描、权限、watcher 或预算无法证明完整性 |
| `content_fidelity` | 最终 change set 是否能提供可展示的内容 diff | 内容因敏感、二进制或大小限制而未保存 |

`AppliedPatchDelta.exact` 是 Codex tracker 的失效开关，不等价于本方案的 `capture_status`：前者回答“受控的实时 unified diff 还能否精确”，后者回答“最终 workspace 变更列表是否完整”。兼容字段 `exact` 若保留，应只作为 `controlled_delta_status == valid` 的别名；不得用它表示 snapshot 是否完整。

## 已核实的现有能力与限制

### WebUI

可复用：

- `api/streaming.py` 已有稳定的 `stream_turn_key`、`stream_id`、session owner guard 和 completed/error/cancel settlement；
- `api/gateway_chat.py` 已有独立的 Gateway worker 生命周期和 Runs API 事件消费；
- run journal 能记录并重放 SSE，Session Manifest 已证明“流式乐观态 + GET 权威态”的模式可行；
- `resolve_trusted_workspace()` 和 anchored file helpers 可复用安全边界思路。

限制：

- WebUI 本地路径不一定是 Gateway、容器或远程 executor 实际修改的路径；
- Gateway 请求目前没有把 `turn_key` 和实际执行 workspace 作为正式变更捕获协议传递；
- Gateway 的 `tool.completed`/`hermes.tool.progress` 当前只保留工具名、ID 和状态，完成结果不能还原 old/new delta；
- `api/streaming.py` 与 `api/gateway_chat.py` 是两个执行 owner，不能只接一个路径。

### Hermes Agent

可复用：

- `tool_start_callback` / `tool_complete_callback` 提供稳定 `tool_call_id`；
- `file_mutation_result_landed()` 和 `_extract_landed_file_mutation_paths()` 能保守判断受控文件工具是否落盘；
- patch 结果可携带 `diff`、`files_modified` 和 resolved path；
- `_turn_file_mutation_paths` 已维护本轮已确认 mutation path；
- `agent/display.py` 的 `LocalEditSnapshot` 已为 `write_file`、`patch`、`skill_manage` 保存 preimage 并生成本地 diff；
- `tools/checkpoint_manager.py` 已验证 shadow Git、独立 index、路径校验、存储配额和回滚的基本做法。

限制：

- `LocalEditSnapshot` 是展示设施，只覆盖少数受控工具，并按进程 `cwd` 解析路径；实际文件工具使用 task/session 的权威 cwd；
- `CheckpointManager` 默认关闭，只在受控写入或被分类为 destructive 的 terminal 前触发；
- checkpoint 默认排除依赖、构建、缓存、VCS、媒体、日志和密钥，跳过大文件及超过 50,000 entries 的目录；
- `ensure_checkpoint()` 只返回 `bool`，没有供 turn 结算持有的 immutable baseline handle；
- `new_turn()` 当前在 Agent model/tool iteration 内调用，不等于一个真实用户 turn 的唯一开始点；
- shared checkpoint store 没有满足本功能所需的 active-baseline pin、同 workspace capture lease 和完整结果语义。

因此应复用实现经验和底层 helper，而不是直接调用现有 `ensure_checkpoint()` 充当 turn baseline。

### Codex app-server

Codex 可直接提供两类高质量数据：

- `fileChange` item：计划/完成状态以及每个 `changes[].path/kind/diff`；
- `turn/diff/updated`：本轮最新聚合 unified diff。

当前 Hermes bridge 的 `on_event` 能收到所有 app-server notification，但 `agent/codex_runtime.py` 只处理 `item/*`，并且把 `fileChange.changes[]` 裁剪为 `kind/path`，完成结果压缩成状态摘要。`turn/diff/updated` 没有进入 Hermes/WebUI 事件链。

需要保留并转发这两类数据，但它们仍不是“所有文件系统变化”的完整来源：Codex 的受控 `fileChange` 路径适合精确展示 apply_patch，普通 command execution 的任意 shell 写入仍要靠 execution-owner baseline 兜底。

## 执行端能力矩阵

| 执行路径 | 当前可见数据 | 完整捕获方案 | 未升级时结果 |
| --- | --- | --- | --- |
| WebUI in-process + Agent local host | callbacks、实际本地 workspace | Agent local snapshot provider + delta callback | 不应声称完整 |
| Agent local + Codex app-server | Codex item notifications、相同 Agent cwd | 转发 Codex diff + Agent local snapshot reconciliation | 只能看到受控 fileChange 摘要 |
| Gateway Runs API，同机或远端 | tool lifecycle、run terminal event | Gateway/Agent 侧 snapshot；新 turn change events | `unknown` |
| Gateway legacy chat-completions | 精简 tool progress | 升级协议或关闭完整捕获声明 | `unknown` |
| Docker/SSH/远程 environment | 取决于 environment adapter | environment-owned snapshot provider | provider 缺失则 `unknown` |
| 外部 MCP 写 workspace | 通常无 old/new | 同一实际 workspace 的 final snapshot | snapshot 不可达则 `unknown` |
| workspace 外写入 | 无统一安全边界 | 明确不在本方案范围 | 不捕获并记录边界 |

WebUI 不得根据 Gateway URL 是 localhost、路径字符串相同或目录碰巧存在，就假设双方共享同一文件系统。只有执行端返回可验证的 `workspace_identity` 和 capture capability 才能声明支持。

## 领域模型

新增实现放在 `integration/turn_file_changes/`。大型上游文件只保留 import、参数传递和单行 hook。

### `TurnCaptureRequest`

WebUI 发给执行端：

```text
protocol_version: 1
client_turn_id: opaque UUID
session_id: str
stream_id: str
turn_key: str
requested_scope: workspace_net
capture_policy: observe | require_complete
```

`client_turn_id` 是跨网络幂等键。执行端不能把 WebUI 传入的任意绝对路径直接当成可信 workspace；实际根目录由 Agent session/environment 解析，并在结果中返回 opaque `workspace_identity`。

### `FileFingerprint`

```text
exists: bool
entry_kind: regular | symlink | directory | other
digest: sha256 | null
size: int | null
mode: int | null
symlink_target_digest: sha256 | null
content_ref: internal blob ref | null
content_visibility: diffable | binary | oversized | sensitive | unreadable
```

不存在与未知必须分开：`exists=false` 表示确认不存在；无法读取使用 diagnostics，不能用 `null` 冒充不存在。

### `AppliedFileDelta`

受控工具实际落盘后的内部账本：

```text
capture_id: str
tool_call_id: str
environment_id: opaque execution-environment identity
path: workspace-relative POSIX path
operation: add | delete | update | move | chmod | type_change
before: FileFingerprint
after: FileFingerprint
move_path: str | null
overwritten_target_before: FileFingerprint | null
landed: confirmed | uncertain
source: hermes_file_tool | codex_file_change | environment_event
sequence: int
```

约束：

- `tool_start` 只可产生 planned change，不可产生 applied delta；
- 每个 delta 必须在实际 I/O 后生成；
- 部分失败返回此前已确认的 delta，并把未确认路径写入 diagnostics；
- `(capture_id, tool_call_id, sequence)` 幂等；
- 路径的内部 key 必须是 `(environment_id, path)`；local、container、SSH 等环境中的同名相对路径绝不能合并；
- 当 `operation=move` 时，`path` 固定为源路径、`move_path` 固定为目标路径；普通 add/update/delete 的 `move_path=null`。目标已存在时以 `overwritten_target_before` 表达其基线，不能把 source/target 调换；
- 只有受控 move 或权威文件系统事件能生成 `operation=move`。

### `ControlledTurnDiffTracker`

这是借鉴 Codex `TurnDiffTracker` 的进程内实时投影，不是 final snapshot 的替代品。每个 active capture 维护：

```text
baseline_by_path[(environment_id, path)]  # 本轮第一次受控修改前的文本状态
current_by_path[(environment_id, path)]   # 累积到当前的受控文本状态
origin_by_current[(environment_id, path)] # 显式 move 后当前路径对应的原始路径
status: valid | invalidated
```

处理 `AppliedFileDelta` 的规则：

1. 首次触及一个路径时写入 `before` 为 baseline，写入 `after` 为 current；后续 delta 只替换 current。
2. `add` 后 `delete`、或 `update` 后恢复 baseline，会从实时净 diff 中抵消；已确认的 delta journal 仍保留用于诊断和归因。
3. 仅当受控 delta 明确给出 source/target 时更新 `origin_by_current` 并渲染 move；snapshot 的同内容 delete/add 不得反向推断 move。
4. `landed=uncertain`、无法取得可靠 preimage/postimage、或写入过程中出现可能部分落盘的 I/O 错误时，调用 `invalidate()`；清空实时 unified diff，后续不再把内存状态伪装为精确结果。
5. final snapshot 与 tracker 的预测状态冲突时，也必须 `invalidate()`，但不得删除此前确认的 delta；若 final snapshot 完整，仍用它生成独立的最终 change set。

tracker 的“文本状态”只适用于 `content_visibility=diffable` 且仍持有受保护内容 ref 的文件；其它文件仍可进入最终 snapshot change list，但不能进入文本 tracker。Tracker 只接收执行端已经提交的 delta，绝不接收 tool start 的计划变更，也不重读 workspace 或调用 Git。渲染时可采用文本 diff 的时间预算与粗粒度降级，但降级后的内容必须与 tracker 的 baseline/current 状态一致。

### `TurnFileChangeSet`

```text
protocol_version: 1
capture_id: str
client_turn_id: str
workspace_identity: str
turn_status: completed | failed | cancelled | abandoned
capture_status: complete | partial | unknown
attribution: controlled | interval | ambiguous
content_fidelity: full | mixed | metadata_only
controlled_delta_status: valid | invalidated | unavailable
changes: [{
  change_event_id: opaque | null,
  path,
  operation,
  before,
  after,
  move_path,
  provenance,
  diff
}]
unified_diff: string | null
unified_diff_complete: bool
diagnostics: {
  excluded_paths,
  unreadable_paths,
  unresolved_paths,
  reconciliation_conflicts,
  watcher_overflow,
  scan_budget_exhausted,
  background_processes_active,
  overlap_capture_ids
}
started_at: float
settled_at: float
```

API 不返回内部 `content_ref`。持久化层也不应把 workspace 全量 old/new 文本直接复制进一行 JSON。

`unified_diff` 是最终 snapshot 结果可用时的展示投影；在 capture 未完成前，SSE 可以另行发送 tracker 生成的 `controlled_unified_diff`。两者必须带各自的状态，前端不能把实时受控 diff 当作最终权威结果。

## Execution-owner snapshot provider

### 接口

在 Hermes Agent 增加独立的 provider 抽象：

```text
begin_capture(capture_request, resolved_environment) -> BaselineHandle
record_applied_delta(handle, delta) -> None
settle_capture(handle, turn_status) -> TurnFileChangeSet
abandon_capture(handle, reason) -> TurnFileChangeSet
release_capture(handle) -> None
```

首批 provider：

- `LocalWorkspaceSnapshotProvider`：使用可移植的 Python 文件系统语义支持 macOS、Linux、原生 Windows 与 WSL；不以 inotify、FSEvents 或 ReadDirectoryChangesW 的单一事件流作为完整性证据；
- `UnsupportedSnapshotProvider`：显式返回 `unknown`；
- 后续为 Docker/SSH 等 environment 增加对应 provider，或在该 environment 内运行同一 snapshot helper。

provider 选择必须基于实际 environment，不得静默回退到 Agent 进程 cwd。

### baseline 建立

1. Agent 解析 task/session 的权威 workspace 和 environment。
2. 分配 `capture_id`，写 durable active-capture record。
3. 启动文件系统 watcher（若平台支持），记录 sequence/overflow；watcher 只是加速与冲突检测，不能单独作为完整性证明。
4. 对范围内所有 entries 做 baseline scan，流式计算指纹。
5. 对扫描期间 watcher 报告的路径重新读取，直到得到稳定 baseline；达到重试/时间预算则 baseline 为 `partial`。
6. baseline handle 持久化成功后，才允许 Agent 开始本轮工具执行；若 `capture_policy=require_complete` 且无法达到 complete，拒绝启动。`observe` 可以继续运行，但 handle 必须从一开始标为 `partial|unknown`。

不能用父目录 mtime 决定“目录未变化所以不递归”。原地覆盖文件内容通常不会改变父目录 mtime。没有可靠 watcher journal 时，最终必须重新检查所有 baseline entries 和新增 entries。

### baseline 内容策略

要在覆盖或删除后生成 old/new 文本 diff，hash 和 metadata 不够。必须在执行前持有下列之一：

- 内容 blob；
- immutable shadow Git tree；
- 文件系统 snapshot/reflink；
- 执行器提供的完整 preimage。

MVP 建议：

- 对所有范围内文件保存指纹，保证变更路径检测；
- 对允许展示且在内容预算内的文本保存 content-addressed preimage blob；
- 对 sensitive/oversized/binary 文件只保存指纹，最终仍记录操作但不生成内容 diff；
- active baseline 存在期间 pin blob/ref，任何 GC 都不得删除；
- baseline 创建失败时仍可运行 Agent，但该 turn 从开始就是 `partial|unknown`。

可以从 `CheckpointManager` 抽取 shadow Git helper，但应新增返回 immutable `BaselineHandle` 的 API，使用每 capture 独立 index/ref 和 active pin。不要复用会被 pruning、默认 exclusions 和 per-iteration dedup 影响的现有 checkpoint ref。

### 最终扫描与合并

1. 停止接受新的受控 delta，记录最终 delta sequence。
2. 等待当前前台工具 I/O 完成；已知后台进程不阻塞无限等待，只写 diagnostics。
3. 对 workspace 做最终全量指纹扫描，或使用无 overflow 的 watcher journal 缩小读取范围后执行完整一致性校验；扫描前后比较根签名和受影响 entry identity，变化时局部重扫并有限次重试，无法收敛即返回 `partial`，不得把非原子遍历称为 complete。
4. 比较 baseline/final fingerprint，得到 add/delete/update/chmod/type change。
5. 用 confirmed delta 补充工具归因、move 关系和部分失败细节。
6. delta 的最终 postimage 与 final scan 冲突时，以 final scan 表示结算时状态，保留冲突诊断并将 `controlled_delta_status` 置为 `invalidated`；final scan 仍完整时，`capture_status` 可以保持 `complete`。
7. 渲染文本 diff；超时可降级为较粗但内容正确的 diff，不可改变 change list。
8. 原子持久化结果后释放 baseline、watcher、临时 index/ref/blob pin。

snapshot 发现的一对 delete/add 即使 digest 相同，也默认保留为 delete+add。inode 可能复用，内容相同也不证明 rename。只有受控 move delta 或权威 filesystem journal 才渲染 rename。

## Hermes Agent 改造

### 真实 turn 生命周期

在 Agent 接收一次真实用户请求、解析 execution environment 后建立 capture；在最外层 `try/finally` 结算。不要挂到当前 `CheckpointManager.new_turn()`，因为它在 model/tool iteration 内重复调用。

所有出口必须进入同一个 finalizer：

- normal completion；
- max iterations / budget exhausted；
- provider error；
- tool error / partial I/O；
- user interrupt；
- process teardown / restart recovery。

### 受控工具 delta

将 `LocalEditSnapshot` 从 display-only helper 提升为 runtime mutation capture：

1. 路径使用 `_resolve_path_for_task()`，与文件工具执行一致；
2. 在统一工具执行出口读取 preimage/postimage，而不是在每个 UI adapter 重复；
3. 复用 mutation result verifier，只对 confirmed landed 写 delta；
4. multi-file patch 按实际成功文件生成多条 delta；
5. display/TUI 继续消费同一结构渲染 inline diff；
6. 新增可选 `turn_file_change_callback(event)`，旧调用方忽略即可。

### `execute_code` 的 scope 规则

`execute_code` 可直接运行任意 Python，并可在脚本中启动子进程；它不能依赖
`tool_complete` 或 terminal 命令解析来枚举写入。最终 snapshot provider 是其完整性
来源，规则按实际执行 environment 区分：

- **`project` 模式、本地 environment**：当前实现把 child CWD 解析到 task/session 的
  workspace（session cwd record → registered override → `TERMINAL_CWD`）。脚本直接写入、
  `subprocess`/`os.system` 写入，以及它经 RPC 调用的文件工具，只要最终落在该 workspace
  根内，都会由同一轮的 baseline/final scan 捕获；是否能生成实时受控 diff 另由 delta
  完整度决定。
- **`strict` 模式的 staging tmpdir**：纯粹写入私有 staging 目录不属于 session execution
  workspace，V1 不报告为用户 workspace 变更。脚本通过 RPC 实际写入 task workspace 时，仍
  按上项捕获。
- **Docker、SSH、Gateway 等 remote environment**：只由其中运行 `execute_code` 的
  execution owner/provider 捕获；V1 provider 未实现时结果为 `unknown`，WebUI 不扫描本地
  同名目录。

无论模式，脚本或子进程在 terminal 之后继续写入，仍遵循本方案的后台进程硬边界：不追加
到已结算 turn，而是由未来的延迟 capture 处理。

### Codex bridge

修改 `agent/codex_runtime.py` 和 app-server session adapter：

- `fileChange.changes[]` 保留 `path/kind/diff`，不再裁剪 diff；
- 转发 `turn/diff/updated` 到新的 turn change callback；
- 校验 notification 的 Codex `threadId/turnId` 与当前 Agent turn；
- `fileChange` 是计划/完成项，公开 schema 只提供 `path/kind/diff`，没有 `AppliedPatchDelta` 的完整 old/new 文本；它本身不能直接构造完整 `AppliedFileDelta`；
- 仅在 Agent runtime 能拿到受控 patch 的 postimage 并验证已落盘时记录 confirmed delta；否则将 Codex item/diff 作为 UI projection，`controlled_delta_status=unavailable`；
- `item/completed` 的 status 只决定该受控 item 的状态，不代表 shell 完整性；
- turn terminal 时由 snapshot provider reconciliation 生成最终 change set。

Codex `fileChange` 的单 item 生命周期应按以下语义桥接：

```text
item/started -> 可选 approval/request -> item/completed
```

Codex 内部在已完成的 patch I/O 后才更新 tracker 并发送 `turn/diff/updated`，但 app-server 的公开契约仅承诺它是“本轮最新聚合 diff”，不承诺它与所有 item 的全局时序或一一对应关系。bridge 必须接受它多次到达、与其它 item 交错或缺失；它只用于实时 UI/乐观投影。WebUI 持久化并通过 GET 返回的 `TurnFileChangeSet` 才是本功能的最终权威结果。

## Gateway 协议改造

### 能力发现

Gateway health/capabilities 增加：

```json
{"turn_file_changes_v1": true}
```

WebUI 只有看到该 capability 才请求和展示“完整捕获”。旧 Gateway 明确显示不支持，不进行本地猜测。

### Runs API 请求

`POST /v1/runs` 增加可选字段：

```json
{
  "client_turn": {
    "protocol_version": 1,
    "client_turn_id": "...",
    "turn_key": "turn:12",
    "stream_id": "..."
  },
  "capture": {"scope": "workspace_net"}
}
```

Gateway 必须把这些字段绑定到自己的 `run_id`，并让 Agent 根据 Gateway session 的实际 cwd/environment 建立 baseline。WebUI 传入的 session workspace 只能作为请求信息；Gateway 必须按自己的信任策略解析，不能直接信任远程绝对路径。

### Runs API 事件

新增：

```text
turn.file_changes.updated    # 可选，受控 delta / Codex diff 的乐观投影
turn.file_changes.completed  # 必须，最终 TurnFileChangeSet
```

terminal 顺序：

```text
Agent settle capture
  -> turn.file_changes.completed
  -> run.completed | run.failed | run.cancelled
```

若 settle 自身失败，仍先发送 `turn.file_changes.completed`，其中 `capture_status=unknown` 和 diagnostics 说明原因。WebUI 以 `(run_id, client_turn_id)` 校验迟到或重复事件。

legacy chat-completions 可后续增加 `hermes.turn.file_changes` SSE，但不作为首批完整实现；未升级时保持 `unknown`。

## WebUI 接入

### 模块边界

新增：

```text
integration/turn_file_changes/
  __init__.py
  models.py       # wire/domain validation
  lifecycle.py    # in-process/gateway adapters and owner guard
  store.py        # SQLite ledger
  handlers.py     # GET API
  rendering.py    # diff truncation/redaction projection
```

接缝改动：

- `api/streaming.py`：构造 `TurnCaptureRequest`、注入 callback、在终态调用统一 settlement adapter；
- `api/gateway_chat.py`：发送 client turn identity、消费 turn change events；
- `api/routes.py`：仅注册 integration GET handler；
- 前端接缝在需要 UI 时只注册 integration asset。

### WebUI 状态层

明确三层状态：

| 状态层 | owner | 生命周期 |
| --- | --- | --- |
| Agent active capture | 实际 execution owner | baseline 建立到 settle/recovery |
| WebUI live projection | `stream_id + turn_key` | 当前 SSE/run journal |
| WebUI durable ledger | integration SQLite store | session/turn 生命周期之后保留 |

Session Manifest 保持不变。Turn file changes 是执行账本，不是 Artifacts/References 派生索引。

### 生命周期不变量

每个事件携带并验证：

```text
profile, session_id, stream_id, turn_key, client_turn_id, capture_id
```

规则：

- WebUI 预先持久化 `pending` ledger row，再启动执行；
- callback 和 Gateway event 必须通过当前 stream owner guard；
- 迟到 worker 只能完成自己的 pending row，不能覆盖新 turn；
- 同一 `capture_id` 重放幂等；
- terminal 事件缺失时，WebUI 将 pending row 结算为 `unknown`，不能自行扫描远端路径；
- cancel 必须等待执行端 capture settlement 或记录明确超时；
- `finally` 只释放本 worker 的 live projection，不能删除已持久化结果。

### 存储

新增 `{HERMES_WEBUI_STATE_DIR}/turn_file_changes.db`，建议表：

```text
turn_change_sets(
  profile, lineage_key, session_id, stream_id, turn_key,
  client_turn_id, capture_id, workspace_identity,
  turn_status, capture_status, attribution, content_fidelity,
  controlled_delta_status, unified_diff, unified_diff_complete,
  diagnostics_json, schema_version, created_at, settled_at
)

turn_file_changes(
  capture_id, change_event_id, path, operation,
  before_json, after_json, move_path,
  provenance_json, diff, sequence
)
```

约束：

- `client_turn_id`、`capture_id` 唯一；
- change set 与 changes 同一事务提交；
- 默认不保存 sensitive file 内容；
- diff 和 diagnostics 有单 turn/单文件大小上限，截断只影响展示，不改变 fingerprint；
- session 删除、retention 和异常启动恢复必须有明确 GC 流程。

### API 与 SSE

新增 integration API：

```text
GET /api/integration/turn_file_changes?session_id=<id>&turn_key=<key>
```

响应错误文案使用中文，并同步：

- `integration/swagger/openapi.json`
- `integration/README.md`

新增 WebUI SSE：

```text
turn_file_changes
```

SSE 是乐观态并写入 run journal；`done` 后 GET 覆盖前端状态。`capture_status=unknown` 必须显示“无法确认”，不能渲染成 0 files changed。

`turn.file_changes.updated` 的 payload 应包含 `projection_source=controlled_tracker|codex_app_server` 与 `controlled_delta_status`。`invalidated` 或 `unavailable` 时，前端停止以该投影更新实时文件数；待 `turn.file_changes.completed` 后以最终 `changes` 覆盖。

## 安全与资源控制

- baseline/CAS 存在 execution owner 的私有状态目录，目录 mode `0700`、文件 `0600`；
- 不在日志、SSE、诊断或 API 返回绝对 workspace 路径、密钥正文或 content blob ref；
- sensitive 文件参与 fingerprint 比较，但 diff 默认 redacted；
- 路径枚举使用 `lstat`/anchored traversal，不跟随逃逸 symlink；
- baseline scan、final scan、hash bytes、blob bytes 和 diff CPU 分别设预算；
- 任一预算耗尽都记录具体维度，`capture_status` 至少为 `partial`；
- active capture 的 baseline pin 不受普通 checkpoint pruning 影响；
- process crash 后保留 active record，重启可尽力结算为 `abandoned`；若无法证明 crash 到 recovery 期间没有外部写入，`attribution=ambiguous`；
- 同 workspace 并发 capture 均可记录各自区间净变化，但在没有独占写入保证时设置 `overlap_capture_ids` 和 `attribution=ambiguous`。
- 共享 watcher journal 或受控 delta 能证明同一外部写入被多个重叠窗口观察到时，执行端生成稳定的 `change_event_id`；它是关联键，不表示唯一因果归属，也不能用于跨 environment 合并同名相对路径。仅有 snapshot 证据时该字段为 `null`，以避免把相同 before/after 指纹误称为同一次写入。

## 实施阶段

### Phase 1：协议、模型和本地 snapshot provider

- 在 Hermes Agent 定义 request/delta/change set schema；
- 实现 local provider、durable baseline handle、active pin 和 recovery；
- 以 `observe` / `require_complete` 覆盖 baseline 无法完整建立时的允许与拒绝路径；
- 覆盖全量 fingerprint、文本 preimage、sensitive/binary metadata-only；
- 实现 `UnsupportedSnapshotProvider`，先建立诚实降级语义。

交付标准：独立 Agent 测试能在 macOS/Linux/Windows/WSL 的支持矩阵中捕获 shell 和文件工具的 workspace 净变化；未实现的远程 environment 只能声明 `unknown`。

### Phase 2：Hermes 受控 delta 与 Codex 复用

- 提升 `LocalEditSnapshot` 到统一 runtime delta；
- 复用 mutation verifier 和 patch landed paths；
- 转发 Codex `fileChange.diff` 与 `turn/diff/updated`；
- 实现 `ControlledTurnDiffTracker`，以 confirmed delta 合并实时净 diff；
- 用 final snapshot 对 Codex/Agent delta 做 reconciliation。

交付标准：受控编辑可流式预览，shell 写入在最终 change set 出现；受控 delta 不确定或二者冲突时实时投影失效，最终结果只由完整 snapshot 定案。

### Phase 3：WebUI in-process 接入

- 新增 `integration/turn_file_changes/` models/store/lifecycle；
- `api/streaming.py` 薄接入 request/callback/settlement；
- completed/error/cancel/replace/teardown 使用同一 owner-guarded finalizer；
- 先提供 ledger、SSE 和 GET，不修改 Session Manifest。

交付标准：in-process backend 可端到端返回 `capture_status=complete`。

### Phase 4：Gateway Runs API

- capability、request fields 和 final events；
- Gateway 绑定实际 environment snapshot provider；
- `api/gateway_chat.py` 透传身份并校验 run/capture；
- legacy transport 明确 unsupported。

交付标准：WebUI 和 Gateway 不共享文件系统时，仍由 Gateway 返回实际 workspace change set。

### Phase 5：UI 与运维

- 展示文件数、capture status、attribution、内容 redaction 和可展开 diff；
- `unknown/partial` 不显示为无变更；
- 增加 retention/GC/recovery 诊断；
- 提供桌面、窄屏、移动端截图和手动验证。

只有 Phase 1-4 完成后，产品才能对所有已声明支持的 chat backend 使用“捕获本轮所有 workspace 净变化”的表述。

## 测试矩阵

统一使用 WebUI 的 `./scripts/test.sh`；Agent 仓库按其自身测试入口执行。

### Snapshot correctness

1. 0/1/many files：add/delete/update/chmod/type change/symlink retarget。
2. 文件原地覆盖但父目录 mtime 不变，仍必须被发现。
3. 同一文件多次更新、更新后还原、新建后删除。
4. 内容相同的 delete+add 不误判 rename。
5. shell redirect、heredoc、脚本、build、MCP/plugin 写入；本地 `execute_code(project)` 的 Python 直接写入、`os.system`/`subprocess` 子进程写入及 RPC 文件工具写入。
6. `execute_code(strict)` 仅写 staging tmpdir 时不产生 session workspace change；其 RPC 写入 task workspace 时产生 change。
7. remote `execute_code` 无 provider 时为 `unknown`，有 provider 时由远端最终 snapshot 结算。
8. `node_modules`、build、cache、binary、large、sensitive 参与 fingerprint。
9. unreadable path、扫描时删除、symlink swap、watcher overflow、预算耗尽。
10. baseline 扫描期间发生变化时重新读取或返回 partial。

### Delta reconciliation

1. write/patch 成功、失败和 multi-file partial failure。
2. confirmed move、move 覆盖已有目标、source/target 单侧失败。
3. delta postimage 与 final snapshot 一致/冲突。
4. 重复 `tool_call_id/sequence` 重放幂等。
5. Codex planned fileChange、completed/failed/declined 和 turn diff 更新。
6. Codex command execution 写文件只能由 snapshot 补齐。
7. 同一路径多次 confirmed delta 仅保留首次 before 与最后一次 after；更新后恢复、add 后 delete 均从实时净 diff 抵消。
8. `landed=uncertain` 或部分 I/O 令 tracker `invalidated` 后，不再输出精确实时 diff；final snapshot 完整时仍恢复最终 change list。
9. local/container/remote environment 的同名相对路径隔离，不得在 tracker 中合并。
10. Codex `fileChange` 仅有 unified diff、无法取得 old/new 时降级为 `controlled_delta_status=unavailable`，但最终 snapshot 仍可 complete。

### Lifecycle and ownership

1. completed/failed/cancelled/max-iterations/provider-error。
2. SSE 断开、worker replace、迟到 callback/finally。
3. Gateway run completed/failed/cancelled，terminal change event 丢失/重复/乱序。
4. process crash 后 active baseline recovery 为 abandoned。
5. 两个 session 同 workspace 并发、外部写入与 overlap diagnostics；共享 journal 能证明同一事件时两个 change set 共享 `change_event_id`，只有 snapshot 时该字段为 `null`，两种情况均为 `ambiguous`。
6. in-process、Codex、Gateway Runs、legacy Gateway、unsupported remote environment。
7. `observe` 在 baseline 不可验证时仍执行但终态为 `unknown`；`require_complete` 拒绝在该环境启动。

### Persistence and API

1. change set 与 files 原子写入，重复 capture 幂等。
2. SSE/run-journal replay 与 GET 最终结果一致。
3. sensitive content 不进入 API、日志或持久化 diff。
4. session delete、retention、active pin 和 orphan baseline GC。
5. 中文 API error、Swagger 和 integration route table 一致。

## 验收标准

- 支持的 execution provider 在预算内完整列出 workspace 范围内的净变化，不依赖工具名或 shell stdout 猜测；
- 不再默认跳过依赖、构建、缓存、二进制、日志或密钥路径；
- hash-only baseline 不被用于声称可恢复 old content；
- Codex `turn/diff/updated` 被复用，但 shell 完整性仍由 execution-owner snapshot 证明；
- 受控实时投影只由已确认 `AppliedFileDelta` 的 baseline/current/origin 状态生成；不确定 I/O 或 reconciliation 冲突后立即失效，不再显示伪精确 diff；
- Codex `fileChange` 的公开 `path/kind/diff` 不被误认为完整的 applied old/new delta；
- Gateway/remote 路径由实际执行端结算，WebUI 不对本地同名路径做推断；
- unsupported、permission error、budget exhaustion、watcher overflow 和 terminal event missing 均返回 `partial|unknown`；
- 变更完整度、进程归因和内容展示完整度分别表达；
- cancel/error/replace/restart 不丢失已确认落盘变化，也不把不确定结果显示为精确空 diff；
- `(profile, session_id, stream_id, turn_key, client_turn_id, capture_id)` 能唯一绑定记录，迟到事件不能污染其它 turn；
- 同一 workspace 的重叠窗口允许重复记录同一变化，以 `overlap_capture_ids` 说明重叠；只有共享 journal 可证明身份时才填写 `change_event_id`，不伪造唯一归因；
- V1 在 execution workspace 根内完整工作；本地支持范围覆盖 macOS、Linux、原生 Windows 与 WSL，未接入的远端环境必须明确降级；
- Session Manifest 与 TurnFileChangeSet 保持独立，可分别重放和修复。

## 明确不采用的方案

- 只解析工具参数或 shell command：无法证明实际写入和部分失败；
- 只消费 `tool_complete_callback`：当前协议缺少统一 preimage/postimage，且不覆盖任意 shell/MCP；
- 只调用现有 `CheckpointManager.ensure_checkpoint()`：触发时机、排除项、返回值和 GC 契约均不满足；
- 只保存 baseline hash，结算时再读取 old content：覆盖/删除后 old content 已不可恢复；
- 只用 Git working tree diff：无法隔离 turn 开始前脏改动，也不覆盖非 Git workspace；
- 只用目录 mtime 做分层扫描：会漏掉原地文件内容更新；
- 只用 Codex `turn/diff/updated`：不覆盖普通 shell command 写入；
- Gateway 模式由 WebUI 扫描本地 workspace：无法证明它与实际执行文件系统相同；
- 根据相同 hash/inode 自动宣称 rename：两者都不是可靠因果证明。

## 参考

- Codex app-server 官方文档（声明 app-server 源码公开、定义 `fileChange` / `turn/diff/updated`）：<https://learn.chatgpt.com/docs/app-server>
- Codex 官方源码，app-server notification schema：<https://github.com/openai/codex/blob/9ded177ce7c1c0bd2047f902936c177612ab3434/codex-rs/app-server-protocol/src/protocol/common.rs>
- Codex 官方源码，turn 级 tracker 的创建与生命周期：<https://github.com/openai/codex/blob/9ded177ce7c1c0bd2047f902936c177612ab3434/codex-rs/core/src/session/turn.rs>
- Codex 官方源码，`AppliedPatchDelta`：<https://github.com/openai/codex/blob/9ded177ce7c1c0bd2047f902936c177612ab3434/codex-rs/apply-patch/src/lib.rs>
- Codex 官方源码，`TurnDiffTracker`：<https://github.com/openai/codex/blob/9ded177ce7c1c0bd2047f902936c177612ab3434/codex-rs/core/src/turn_diff_tracker.rs>
- 腾讯 CodeBuddy WorkBuddy，变更面板与按任务查看：<https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Right-Sidebar>
- 腾讯 CodeBuddy WorkBuddy，workspace、独立任务与并行任务：<https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Task-Bar>
- Hermes Agent：`agent/display.py`、`agent/tool_executor.py`、`agent/tool_dispatch_helpers.py`、`agent/tool_result_classification.py`、`agent/codex_runtime.py`、`agent/transports/codex_app_server_session.py`、`tools/checkpoint_manager.py`
- Hermes WebUI：`api/streaming.py`、`api/gateway_chat.py`、`integration/session_manifest/manifest.py`、`api/workspace.py`
