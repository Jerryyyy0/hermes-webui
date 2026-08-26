---
name: manifest-artifacts-references-analysis
overview: 完整梳理 manifest 系统 artifacts/references 的来源映射 + 修复附件误入 + 收窄 reconcile 阶段仅保留 ASSISTANT_PROSE/MEDIA，移除路 5 通用路径和路 6 发现类工具 diff 路径
todos:
  - id: revert-reconcile-external
    content: 回滚 _merge_reconcile_artifacts_for_turn() 中 ASSISTANT_PROSE 的外部文件兜底 (line 1450-1453)
    status: completed
  - id: revert-row-to-wire-external
    content: 回滚 _row_to_wire() 中 ASSISTANT_PROSE 的外部文件兜底，恢复为仅 MEDIA
    status: completed
  - id: simplify-assistant-prose
    content: 简化 _paths_from_assistant_prose()，移除冗余 _paths_from_delivery_prose() 调用
    status: completed
  - id: remove-reconcile-general
    content: 移除 _reconcile_candidate_paths() 通用路径 (line 1389-1404)
    status: completed
  - id: remove-reconcile-discovery
    content: 移除 _reconcile_candidate_paths() 发现类工具 diff 路径 (line 1386-1387)
    status: completed
  - id: update-tests
    content: 测试更新：外部/附件/搜索/终端路径不进入；保留 write 工具 + assistant 正文 workspace 内路径
    status: completed
  - id: run-all-tests
    content: 运行全部 manifest 测试确认无回归
    status: completed
isProject: false
---

## 一、artifacts（成果）的完整来源

### 路 1：写入类工具参数路径

- **触发工具**：`ARTIFACT_MUTATION_TOOLS` = `write_file` / `create_file` / `edit_file` / `patch` / `apply_patch` / `mcp_filesystem_write_file` / `mcp_filesystem_edit_file`
- **路径来源**：`_paths_from_args()` 提取 `path` / `file_path` / `target` / `destination` / `filename` / `paths[]` / `edits[].path` 等参数键；`_paths_from_diff_text()` 提取统一 diff 格式（`+++ b/path` / `*** Add File:` 等）
- **过滤**：无额外门槛，直接写入 artifacts
- **特殊处理**：如果路径是 profile skills 目录下的 `SKILL.md`，转为 skill artifact（`preview: "skill"`）
- **代号**：`_extract_manifest_records()` 主提取阶段
- **改动影响**：无

### 路 2：技能管理类写入

- **触发工具**：`skill_manage`（action 为 `create` / `edit` / `patch` / `write_file`）
- **路径来源**：`result.path`（如 `productivity/my-coffee`）或 `args.name`
- **过滤**：需 `HERMES_INTEGRATION=1`；profile skills 目录下 `SKILL.md` 存在；非 `in_progress` 状态
- **输出类型**：skill artifact（`preview: "skill"`）
- **代号**：`_extract_manifest_records()` 主提取阶段
- **改动影响**：无

### 路 3：MEDIA 标签

- **触发来源**：assistant 消息中的 `MEDIA:<path>` 标签
- **路径来源**：`_paths_from_assistant_media()` 正则提取，排除 `https://` 远程 URL
- **过滤**：无额外门槛（直接写 artifacts）；wire 阶段走 `_session_media_preview_path()` 外部文件兜底
- **代号**：`_collect_media_artifact_events()` → 主提取 + reconcile
- **改动影响**：无

### 路 4：assistant 正文宽泛正则扫描（本次改动核心）

- **触发来源**：所有 `role=assistant` 消息的正文
- **调用入口**：`_merge_reconcile_artifacts_for_turn()` → `_collect_assistant_prose_artifact_events()`（line 1427）

#### 步骤 1：路径收集（`_paths_from_assistant_prose()`，line 278-288）

```python
def _paths_from_assistant_prose(text: str, workspace: Path) -> list[str]:
    if not text or not isinstance(text, str):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    # 直接宽泛正则扫描，无需交付上下文前置
    for candidate in _path_candidates_from_text(text, workspace, source='assistant_prose'):
        if candidate.path not in seen and candidate.confidence != 'contextual':
            seen.add(candidate.path)
            paths.append(candidate.path)
    return paths
```

**修复前的两层结构**：第一层 `_paths_from_delivery_prose()`（交付标签 + 上下文）+ 第二层宽泛正则。但宽泛正则已覆盖交付标签能匹配到的所有路径类型（绝对/相对/代码块/链接），也覆盖交付上下文全文扫描的候选，因此第一层完全冗余。路 5 被移除后，`_paths_from_delivery_prose` 已无其他消费者。

> **修复前曾包含的冗余层**（将随修复移除）：
> - `_paths_from_delivery_prose()`（line 250-275）：交付标签路径 + 标签行内 markdown 链接 + 交付上下文全文扫描
> - 三类子来源被宽泛正则完全覆盖，无遗漏

**宽泛正则收集的 5 种 candidate 类型**：见 `_path_candidates_from_text()`（line 217-234）：

| 正则 | 示例 | confidence |
|------|------|-----------|
| `_CODE_SPAN_RE` | `` `output.csv` `` | `explicit` |
| `_MARKDOWN_LINK_LABEL_RE` | `[file](url)` | `explicit` |
| `_BROAD_ABSOLUTE_PATH_RE` | `/workspace/output.csv` | `broad` |
| `_BROAD_RELATIVE_PATH_RE` | `./src/main.py` | `broad` |
| `_BROAD_BASENAME_RE` | `report.docx` | `contextual`（过滤掉） |

#### 步骤 2：事件创建（`_collect_assistant_prose_artifact_events()`，line 341-369）

对每个 `_paths_from_assistant_prose()` 返回的路径，创建 `ToolEvent`：

```python
ToolEvent(
    name=ASSISTANT_PROSE_ARTIFACT_SOURCE,
    args={'path': path},
    assistant_msg_idx=msg_idx,
    source='assistant_prose',
)
```

所有这些 event 会进入 reconcile 阶段处理。

#### 步骤 3：reconcile 阶段处理（`_reconcile_candidate_paths()`，line 1380-1382）

```python
# line 1380-1382
if name == ASSISTANT_PROSE_ARTIFACT_SOURCE:
    promoted = _promote_artifact_candidates(text_candidates, event, workspace)
    return args_paths + [path for path in promoted if path not in args_paths]
```

对 ASSISTANT_PROSE event，返回 `args_paths`（来自 `_paths_from_assistant_prose()`）+ `promoted_paths`（来自 `_promote_artifact_candidates()` 对 text_candidates 的提升）。

**注意**：`args_paths` 直接纳入返回，不经过 `_promote_artifact_candidates()` 的 evidence 检查。`_promote_artifact_candidates()` 的过滤只对 `promoted_paths`（即 text_candidates 中被提升的部分）生效，这是一个额外补充路径。

#### 步骤 4：证据门槛（`_promote_artifact_candidates()` → `has_evidence()`，line 1336-1348）

**对 ASSISTANT_PROSE 事件**（line 1340-1343）：

```python
if event.name == ASSISTANT_PROSE_ARTIFACT_SOURCE:
    if candidate.confidence != 'contextual':
        return True          # explicit / broad → 直接通过
    return _has_delivery_context(candidate.context)  # contextual → 二次确认交付上下文
```

| confidence | 来源 | has_evidence 判定 |
|-----------|------|------------------|
| `explicit` | `_CODE_SPAN_RE`、`_MARKDOWN_LINK_LABEL_RE` | 直接通过 |
| `broad` | `_BROAD_ABSOLUTE_PATH_RE`、`_BROAD_RELATIVE_PATH_RE` | 直接通过 |
| `contextual` | `_BROAD_BASENAME_RE`（仅在交付上下文行中收集） | 需二次确认 `_has_delivery_context(candidate.context)` |

> **说明**：`contextual` 收集时已要求所在行有 `_has_delivery_context`，但这是行级别判断。二次确认是 candidate 级别精确匹配，安全冗余。详见第四章 confidence 三层分级分析。

#### 步骤 5：文件存在验证（reconcile 阶段 `_merge_reconcile_artifacts_for_turn()`，line 1450）

```python
# 回滚前（改动 3 引入，将被回滚）：
if not _artifact_path_is_real(workspace, path):
    if event.name != ASSISTANT_PROSE_ARTIFACT_SOURCE:
        continue
    if _session_media_preview_path(workspace, path, 'file') is None:
        continue

# 回滚后：
if not _artifact_path_is_real(workspace, path):
    continue
```

回滚后仅验证 workspace 内文件存在，外部文件不进入 artifacts。外部文件展示仅由路 3（MEDIA 标签）控制。

#### 步骤 6：wire 阶段（`_row_to_wire()`，line 1668-1673）

```python
# 回滚前（改动 3 引入，将被回滚）：
if source_tool in (MEDIA_ARTIFACT_SOURCE, ASSISTANT_PROSE_ARTIFACT_SOURCE):
    media_path = _session_media_preview_path(...)

# 回滚后：
if source_tool == MEDIA_ARTIFACT_SOURCE:
    media_path = _session_media_preview_path(...)
```

回滚后 ASSISTANT_PROSE 路径仅通过 `_file_preview_path()` 序列化（仅 workspace 内文件）。

#### 完整调用链

```
_merge_reconcile_artifacts_for_turn()
  │
  └─ _collect_assistant_prose_artifact_events()        [line 1427]
       │
       └─ _paths_from_assistant_prose(text, ws)        [line 278]
            │
            └─ _path_candidates_from_text()  [line 186]  ← 宽泛正则（唯一来源）
                 ├─ _CODE_SPAN_RE            → explicit
                 ├─ _MARKDOWN_LINK_LABEL_RE  → explicit
                 ├─ _BROAD_ABSOLUTE_PATH_RE  → broad
                 ├─ _BROAD_RELATIVE_PATH_RE  → broad
                 └─ _BROAD_BASENAME_RE       → contextual（过滤）
       │
       └─ ToolEvent(name=ASSISTANT_PROSE_ARTIFACT_SOURCE)

  │
  └─ for each ASSISTANT_PROSE event:
       _reconcile_candidate_paths(event, ws)            [line 1380]
         └─ return args_paths + promoted_paths

       for path in result:
         if not _artifact_path_is_real(ws, path):       [line 1450] ← 回滚后仅 workspace 内
           continue
         _merge_file_records(artifacts, ...)

  │
  └─ _row_to_wire()                                     [line 1668]
       └─ _file_preview_path() 仅 workspace 内文件

- **改动影响**：核心改动，见改动 1/2（保留）+ 改动 3（回滚）

### ~~路 5：reconcile 阶段 — 非只读工具的交付输出（将被移除）~~

~~之前此路允许 terminal/shell/execute_code/str_replace 等工具的输出路径通过 deliver
y_context 门槛进入 artifacts。~~ **将被移除**：`_reconcile_candidate_paths()` line 1389-1404 的通用路径改为直接返回 `[]`，这些工具不再通过 reconcile 产出 artifacts。

### ~~路 6：reconcile 阶段 — 发现类工具的 diff 证据（将被移除）~~

~~之前此路允许 discovery 工具在搜索结果中发现 diff 格式路径时进入 artifacts。~~ **将被移除**：`_reconcile_candidate_paths()` line 1386-1387 对 `_is_reference_only_tool` 改为直接返回 `[]`，发现/搜索类工具完全不产 artifacts。

---

## 二、references（参考）的完整来源

### 路 R1：技能查看

- **触发工具**：`REFERENCE_SKILL_TOOLS` = `skill_view`
- **路径来源**：`args.name`（技能名）
- **过滤**：`HERMES_INTEGRATION=1` 且 SkillHub 可用
- **输出类型**：skill reference（`preview: "skill"`）
- **代号**：`_extract_manifest_records()` 主提取阶段
- **改动影响**：无

### ~~路 R2（已移除）：文件读取类工具~~

- **原触发工具**：`REFERENCE_READ_TOOLS`（`read_file` / `open_file` / `view_file` / `mcp_filesystem_read_file`）+ `REFERENCE_DIR_TOOLS`（`list_dir` / `mcp_filesystem_list_directory`）
- **原输出**：file/dir references
- **删除位置**：改动 4a（`_extract_manifest_records()`）+ 改动 4b（`_reconcile_candidate_paths()` 提前拦截）
- **改动影响**：完全移除

---

## 三、不产生任何产出的工具类型

| 工具类型 | 说明 |
|---------|------|
| `REFERENCE_READ_TOOLS`（读文件）| 改动后彻底移除 |
| `REFERENCE_DISCOVERY_TOOLS`（搜索发现）| 主提取阶段不处理；reconcile 阶段仅在有 diff 证据时产 artifact，不产 reference |
| `REFERENCE_DIR_TOOLS`（列目录）| 随读工具一起移除 |

---

## 四、confidence 三层分级详细逻辑

`confidence` 在 `_path_candidates_from_text()` 收集阶段赋值，在 `_promote_artifact_candidates()` → `has_evidence()` 提升阶段使用。

### 第一层：收集阶段（`_path_candidates_from_text()`，line 217-234）

| 正则类型 | 匹配示例 | confidence | 收集条件 |
|---------|---------|-----------|---------|
| `_CODE_SPAN_RE` | `` `output.csv` `` | `explicit` | **所有行都收集** |
| `_MARKDOWN_LINK_LABEL_RE` | `[file](url)` | `explicit` | **所有行都收集** |
| `_BROAD_ABSOLUTE_PATH_RE` | `/workspace/output.csv` | `broad` | **所有行都收集** |
| `_BROAD_RELATIVE_PATH_RE` | `./src/main.py` | `broad` | **所有行都收集** |
| `_BROAD_BASENAME_RE` | `report.docx` | `contextual` | 仅当**当前行有交付上下文** |

为什么 basename 需要交付上下文？`report.docx` 这种裸文件名太常见，"你可以查看 report.docx"和"这是 report.docx 文档"都可能出现，只有前者是交付意图。不加限制会收集大量随意提及的文件名。

### 第二层：提升阶段（`_promote_artifact_candidates()` → `has_evidence()`，line 1336-1348）

```python
if event.name == ASSISTANT_PROSE_ARTIFACT_SOURCE:
    if candidate.confidence != 'contextual':
        return True          # explicit / broad → 直接通过
    return _has_delivery_context(candidate.context)  # contextual → 二次确认
```

为什么 contextual 还要二次确认？收集时的 `has_delivery_context` 是**行级别**判断——某一行有"已生成"关键词，该行所有的 basename 都被收集。二次确认是**candidate 级别**精确匹配，确保该候选确实在交付上下文中。这是一道安全冗余。

### 完整决策树

```
assistant 正文中的路径片段
    │
    ├─ 是代码块/链接标签？
    │   → confidence=explicit → has_evidence: 直接通过 → _artifact_path_is_real()
    │
    ├─ 是绝对路径或相对路径？
    │   → confidence=broad → has_evidence: 直接通过 → _artifact_path_is_real()
    │
    └─ 是裸文件名（如 report.docx）？
        → 这一行有交付上下文吗？
            ├─ 没有 → 不收集
            └─ 有 → confidence=contextual → has_evidence: 二次确认交付上下文
                                              → _artifact_path_is_real()
```

**一句话总结**：`explicit`（代码块/链接）和 `broad`（绝对/相对路径）是"模式本身就够可信"，`contextual`（裸基名）是"模式不够可信，需要交付上下文加持"。

---

## 五、过滤层级汇总

| 层级 | 作用 | 适用于 |
|------|------|-------|
| 工具白名单 | 只有特定工具产 artifact/reference | 路 1/2/R1 |
| 交付上下文 | 宽泛正则 candidate 的提升门槛 | 路 4（contextual）/ 路 5 |
| `_artifact_path_is_real()` | workspace 内文件存在验证 | 路 4/5/6 reconcile |
| `_session_media_preview_path()` | 外部文件存在验证 | 仅路 3（MEDIA）wire 阶段 |
| `ARTIFACT_IGNORE_RE` | 排除 .git/node_modules 等 | 路径解析阶段全局 |
| `_file_preview_path()` / `_row_to_wire()` | 最终 wire 过滤 | 所有 file artifact（仅 workspace 内） |

---

## 六、实际案例验证（session: c05b5c2395b7）

manifest 返回结果：

```json
{
  "artifacts": [
    {"path": "/home/hermeswebui/.hermes/webui/attachments/.../姜远皓_xxx.pdf", "source_tool": "assistant_prose"},  // ← 误入！
    {"path": "/home/hermeswebui/.hermes/webui/attachments/.../张炜杰_xxx.pdf", "source_tool": "assistant_prose"},  // ← 误入！
    {"path": "document-format-skills/pdf-text-extraction", "source_tool": "skill_manage"},
    {"path": "候选人简历汇总及建议.md", "source_tool": "write_file"}
  ],
  "references": [
    {"path": "ocr-and-documents", "source_tool": "skill_view"}
  ]
}
```

分析：
- `候选人简历汇总及建议.md` → 路 1，write_file 产出 ✓
- `document-format-skills/pdf-text-extraction` → 路 2，skill_manage 产出 ✓
- `ocr-and-documents` → 路 R1，skill_view 产出 ✓
- **两个 `.pdf` 附件路径** → 路 4，被改动 3 的外部文件兜底误收入 ✗

---

## 七、附件误入 artifacts 的根因

### 完整链路

```
assistant 正文中提及附件:
  "/home/hermeswebui/.hermes/webui/attachments/c05b5c2395b7/姜远皓_xxx.pdf"

  ↓ [改动2] _paths_from_assistant_prose() 宽泛正则扫描
     _BROAD_ABSOLUTE_PATH_RE 匹配到该路径
     → candidate confidence = 'broad'
     → 路径不在 workspace 下 → 解析为外部绝对路径

  ↓ _collect_assistant_prose_artifact_events()
     → 创建 ASSISTANT_PROSE_ARTIFACT_SOURCE event

  ↓ _reconcile_candidate_paths()
     ASSISTANT_PROSE → 返回 args_paths + promoted_paths

  ↓ [改动3修改处1] _merge_reconcile_artifacts_for_turn() line 1450
     _artifact_path_is_real(ws, path) → False (不在 workspace 下)

     # 改动3新增的逻辑:
     if event.name != ASSISTANT_PROSE_ARTIFACT_SOURCE: continue  ← 不跳过
     if _session_media_preview_path() is None: continue          ← 文件存在，不跳过
     → 路径进入 artifacts ✗ (副作用!)

  ↓ [改动3修改处2] _row_to_wire()
     _file_preview_path() → None (不在 workspace)
     source_tool in (MEDIA, ASSISTANT_PROSE) → True  ← 改动3新增
     _session_media_preview_path() → 文件存在 → 序列化 ✗ (副作用!)
```

### 核心矛盾

为 ASSISTANT_PROSE 加的外部文件兜底没有区分"成果产出的外部文件"和"输入引用的外部文件"。用户上传的附件路径被宽泛正则收集后，因为磁盘上真实存在，被当作成果展示。

---

## 八、修复方案

**原则**：artifacts 仅由以下来源产出：
- 写入类工具参数路径（路 1）
- 技能管理类写入（路 2）
- MEDIA 标签（路 3）
- assistant 正文中 workspace 内的文件路径（路 4，无外部文件兜底）

其他所有 reconcile 阶段路径（路 5 通用路径、路 6 发现类 diff 路径）全部移除。

### 修复 A1：回滚 `_merge_reconcile_artifacts_for_turn()` external fallback

**文件**：`integration/session_manifest/manifest.py`，改动 3 修改处 1

```python
# 现状（改动3 引入，line 1449-1453）
            if not _artifact_path_is_real(workspace, path):
                if event.name != ASSISTANT_PROSE_ARTIFACT_SOURCE:
                    continue
                if _session_media_preview_path(workspace, path, 'file') is None:
                    continue

# 回滚为原始逻辑
            if not _artifact_path_is_real(workspace, path):
                continue
```

### 修复点 2：回滚 `_row_to_wire()` external fallback

**文件**：`integration/session_manifest/manifest.py`，改动 3 修改处 2

```python
# 现状（改动3 引入）
    if source_tool in (MEDIA_ARTIFACT_SOURCE, ASSISTANT_PROSE_ARTIFACT_SOURCE):
        media_path = _session_media_preview_path(workspace, rel, entry_kind)
        ...

# 回滚为原始逻辑（仅 MEDIA）
    if source_tool == MEDIA_ARTIFACT_SOURCE:
        media_path = _session_media_preview_path(workspace, rel, entry_kind)
        ...
```

### 修复后效果

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| assistant 说 "文件在 /workspace/output.csv"（workspace 内，文件存在） | artifacts ✓ | artifacts ✓ |
| assistant 提及附件 `/home/.../attachments/xxx.pdf`（外部，文件存在） | artifacts ✗ (误入) | 不出现 ✓ |
| assistant 说 "MEDIA:/external/report.pdf"（MEDIA 标签） | artifacts ✓ | artifacts ✓ |
| assistant 说 "可以参考 /tmp/test.txt"（外部，无交付上下文，文件存在） | artifacts ✗ (误入) | 不出现 ✓ |

### 保留不改的部分

- **改动 1**：`_promote_artifact_candidates()` 中非 contextual 直接通过 — 保留
- **改动 2**：`_paths_from_assistant_prose()` 中宽泛正则收集 — 保留
- **改动 4**：read_file 等不再进 references — 保留
- **正则修改**：fullwidth 冒号 `：` 加入排除字符集 — 保留

### 修复 B1：移除路 5 — reconcile 通用路径

**文件**：`integration/session_manifest/manifest.py` — `_reconcile_candidate_paths()` line 1389 之后

```python
# 现状（line 1389-1404）
    delivery_paths: list[str] = []
    for blob in blobs:
        delivery_paths.extend(_paths_from_delivery_prose(blob, workspace))
    promoted_paths = _promote_artifact_candidates(
        text_candidates + command_output_candidates,
        event, workspace,
    )
    paths: list[str] = []
    seen: set[str] = set()
    for path in args_paths + diff_paths + delivery_paths + promoted_paths:
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths

# 改为：通用路径直接返回空
    return []
```

### 修复 B2：移除路 6 — 发现类工具 diff 证据

**文件**：`integration/session_manifest/manifest.py` — `_reconcile_candidate_paths()` line 1386-1387

```python
# 现状
    if _is_reference_only_tool(name):
        return diff_paths if _event_has_diff_evidence(event, workspace) else []

# 改为
    if _is_reference_only_tool(name):
        return []
```

### 修复 C：简化 `_paths_from_assistant_prose()`（移除冗余 `_paths_from_delivery_prose()` 调用）

**文件**：`integration/session_manifest/manifest.py` — `_paths_from_assistant_prose()` line 278-288

```python
# 现状（改动 2 引入，双层结构）
def _paths_from_assistant_prose(text, workspace):
    if not text or not isinstance(text, str):
        return []
    paths = _paths_from_delivery_prose(text, workspace)  # 冗余
    seen = set(paths)
    for candidate in _path_candidates_from_text(text, workspace, source='broad_scan'):
        if candidate.path not in seen and candidate.confidence != 'contextual':
            seen.add(candidate.path)
            paths.append(candidate.path)
    return paths

# 改为：直接宽泛正则，移除冗余交付标签层
def _paths_from_assistant_prose(text, workspace):
    if not text or not isinstance(text, str):
        return []
    paths = []
    seen = set()
    for candidate in _path_candidates_from_text(text, workspace, source='assistant_prose'):
        if candidate.path not in seen and candidate.confidence != 'contextual':
            seen.add(candidate.path)
            paths.append(candidate.path)
    return paths
```

**原因**：`_paths_from_delivery_prose()` 的三类子来源（交付标签路径、标签行内 markdown 链接、交付上下文全文扫描）被宽泛正则的 5 种 candidate 类型完全覆盖。路 5 被移除后，`_paths_from_delivery_prose` 已无其他消费者。

### 修复后效果（更新版）

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| write_file 写入 /workspace/file.txt | artifacts ✓ | artifacts ✓ |
| assistant 说 "文件在 /workspace/output.csv"（文件存在） | artifacts ✓ | artifacts ✓ |
| assistant 说 "MEDIA:/external/report.pdf" | artifacts ✓ | artifacts ✓ |
| skill_manage 创建 skill | artifacts ✓ | artifacts ✓ |
| assistant 提及附件 `/home/.../attachments/xxx.pdf`（外部，文件存在） | artifacts ✗ (误入) | 不出现 ✓ |
| terminal "已保存: /workspace/report.docx"（文件存在） | artifacts ✓ | 不出现 ✓ |
| execute_code "输出文件：/workspace/out.json"（文件存在） | artifacts ✓ | 不出现 ✓ |
| session_search 搜到 workspace 内文件路径 | artifacts ✗ (误入) | 不出现 ✓ |
| rg/grep/glob 搜到 diff 格式路径 | artifacts ✓ | 不出现 ✓ |

### 测试更新

**保留的现有测试**：
- write_file 相关所有测试
- skill_manage 相关所有测试
- MEDIA 标签相关测试
- assistant 正文交付标签相关测试
- assistant 正文宽泛正则路径相关测试（改动一新增）

**需要更新的现有测试**（原来依赖路 5）：
- `test_build_session_manifest_terminal_pandoc_output_arg_artifact` → 期望 artifacts 为空
- `test_build_session_manifest_terminal_ls_path_candidate_not_artifact` → 期望 artifacts 为空（已是）
- `test_build_session_manifest_execute_code_delivery_output` → 期望仅 ASSISTANT_PROSE 路径
- `test_build_session_manifest_execute_code_delivery_skips_missing_and_external` → 期望 artifacts 为空
- `test_build_session_manifest_reconcile_str_replace_path` → str_replace 进入路 5，期望为空
- `test_build_session_manifest_reconcile_result_path_requires_existing_file` → 期望为空

**新增测试**：
- session_search 等搜索工具搜到 workspace 内文件 → 不出现在 artifacts
- terminal 交付输出路径 → 不出现在 artifacts
- execute_code 交付输出路径 → 不出现在 artifacts

### 保留不改的部分

- **改动 1**：`_promote_artifact_candidates()` 中非 contextual 直接通过 — 保留
- **改动 2**：`_paths_from_assistant_prose()` 中宽泛正则收集（简化后，移除 `_paths_from_delivery_prose` 调用） — 保留
- **改动 4**：read_file 等不再进 references — 保留
- **正则修改**：fullwidth 冒号 `：` 加入排除字符集 — 保留

---

## 九、reconcile 阶段全貌（修复后）

```
_reconcile_candidate_paths(event, workspace):
  │
  ├─ 写入类工具 (ARTIFACT_MUTATION_TOOLS / skill_manage) → return []  (主提取已处理)
  │
  ├─ MEDIA_ARTIFACT_SOURCE → return args_paths               (路 3)
  │
  ├─ ASSISTANT_PROSE_ARTIFACT_SOURCE                         (路 4)
  │   └─ return args_paths + promoted_paths
  │
  ├─ REFERENCE_READ_TOOLS → return []                        (改动 4b)
  │
  ├─ _is_reference_only_tool → return []                     (修复 B2)
  │
  └─ 通用路径 (terminal/shell/execute_code/str_replace等) → return []  (修复 B1)
```

---

## 十、reconcile 阶段修复对比

| reconcile 来源 | 修复前 | 修复后 |
|---------------|--------|--------|
| MEDIA 标签 | artifacts ✓ | artifacts ✓ |
| ASSISTANT_PROSE（宽泛正则） | artifacts ✓ | artifacts ✓ |
| terminal/shell/execute_code | 路 5 → artifacts ✓ | 不产出 |
| str_replace 等 | 路 5 → artifacts ✓ | 不产出 |
| session_search 等未分类工具 | 路 5 → artifacts ✗ | 不产出 |
| rg/grep/glob 发现类 | 路 6 → artifacts（diff证据） | 不产出 |
| REFERENCE_READ_TOOLS | 改动 4b → 不产出 | 不产出 |

---

## 十一、近期改动影响矩阵（更新版）

| 改动 | 影响的路 | 状态 |
|------|---------|------|
| 1. `has_evidence()` 非 contextual 直接通过 | 路 4 | ✅ 保留 |
| 2. `_paths_from_assistant_prose()` 简化（移除 `_paths_from_delivery_prose`） | 路 4 | 🔄 简化 ← 修复 C |
| 3. `_row_to_wire()` MEDIA+ASSISTANT_PROSE 外部文件兜底 | 路 3/4 | 🔄 回滚 ← 修复 A |
| 4a. 删除 REFERENCE_READ_TOOLS 引用 block | ~~R2~~ | ✅ 保留 |
| 4b. reconcile 提前拦截 REFERENCE_READ_TOOLS | ~~R2~~+路 5/6 | ✅ 保留 |
| 5. 移除路 5 通用 reconcile 路径 | 路 5 | 🔄 移除 ← 修复 B1 |
| 6. 移除路 6 发现类工具 diff 路径 | 路 6 | 🔄 移除 ← 修复 B2 |

---

## 十二、`manifest.turns[]` 的构建逻辑

### turn_key 如何对应每一轮对话

**核心规则**：`turn_key` = `"turn:<user_msg_idx>"`，其中 `<user_msg_idx>` 是 `role=user` 消息在 `messages[]` 数组中的索引位置（从 0 开始）。

**`_message_turns()`（line 1077-1092）**：

```python
def _message_turns(messages: list) -> list[dict[str, Any]]:
    turns = []
    for idx, message in enumerate(messages or []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        # 上一轮的 end 锁定在当前 user 消息之前
        if turns:
            turns[-1]['end_msg_idx'] = idx - 1
        # 创建新一轮
        turns.append({
            'turn_key': f'turn:{idx}',     # ← 直接用 user 消息的索引
            'user_msg_idx': idx,
            'start_msg_idx': idx,
            'end_msg_idx': len(messages or []) - 1,  # 先假设到消息末尾
            'artifacts': [],
            'references': [],
        })
    return turns
```

**每一轮的边界**：
- `start_msg_idx` = user 消息的索引（包含）
- `end_msg_idx` = 下一个 user 消息的前一个索引（或消息数组末尾）
- 一轮包含：user 消息 + 本轮所有的 assistant/tool 回复

**示例说明**：

```
messages[] 中的消息序列:
  [0] role=user      → 触发 turn:0
  [1] role=assistant
  [2] role=tool
  [3] role=assistant  ← turn:0 覆盖 [0, 4]
  [4] role=tool
  [5] role=user      → 触发 turn:5；turn:0 的 end 锁定为 4
  [6] role=assistant
  [7] role=tool
  [8] role=user      → 触发 turn:8；turn:5 的 end 锁定为 7
  [9] role=assistant  ← turn:8 覆盖 [8, 9]（假设消息到此为止）
```

**注意**：末尾的 system 消息（如 CONTEXT COMPACTION / compression_anchor 等）不会触发新的 turn，因为它们的 `role` 不是 `user`。

### 事件如何归属到 turn

**`_turn_key_for_event()`（line 1095-1106）**：

```python
def _turn_key_for_event(event: ToolEvent, turns: list[dict[str, Any]]) -> str | None:
    idx = event.assistant_msg_idx     # 优先取 assistant 消息的索引
    if idx is None:
        idx = event.tool_msg_idx      # 兜底取 tool 消息的索引
    if isinstance(idx, bool) or not isinstance(idx, int):
        return None
    for turn in reversed(turns):      # 从后往前遍历
        start = turn.get('start_msg_idx')
        end = turn.get('end_msg_idx')
        if isinstance(start, int) and isinstance(end, int) and start <= idx <= end:
            return str(turn.get('turn_key') or '')
    return None
```

**归属逻辑**：
1. 取 event 的 `assistant_msg_idx`（优先）或 `tool_msg_idx`（兜底）
2. 从后往前遍历所有 turns，找到第一个 `start_msg_idx <= idx <= end_msg_idx` 的 turn
3. 返回该 turn 的 `turn_key`

### wire 阶段输出格式

**`_turn_to_wire()`（line 1704-1717）** 序列化为：

```json
{
    "turn_key": "turn:0",
    "artifacts": [
        {"path": "notes.txt", "preview": "file", "source_tool": "write_file", "profile": "default"}
    ],
    "references": [
        {"path": "my-coffee", "preview": "skill", "source_tool": "skill_view"}
    ]
}
```

### turns[] 在 build_session_manifest 中的完整构建流程

```
messages[] (全部消息，含 user/assistant/tool/system)
  │
  ├─ _message_turns(messages)
  │   → [{turn_key: 'turn:0', start:0, end:4, artifacts:{}, refs:{}},
  │       {turn_key: 'turn:5', start:5, end:9, artifacts:{}, refs:{}}]
  │
  ├─ _collect_tool_events(messages) + _collect_media_artifact_events()
  │   → [ToolEvent(assistant_msg_idx=1, name='write_file', ...),
  │       ToolEvent(assistant_msg_idx=3, name='MEDIA', ...), ...]
  │
  ├─ _extract_manifest_records(events, ws, messages)
  │   └─ for each event:
  │       turn_key = _turn_key_for_event(event, turns)   ← 归属到轮次
  │       turn_rows[turn_key].artifacts.add(path)        ← 写入该轮的 artifacts
  │       turn_rows[turn_key].references.add(path)       ← 写入该轮的 references
  │   └─ 返回 (session_artifacts, session_references, turn_list)
  │
  ├─ _apply_turn_reconcile_to_manifest_records()
  │   └─ _merge_reconcile_artifacts_for_turn()  ← 逐轮做宽泛正则 reconcile
  │       └─ 将 reconcile 发现的路径加入该轮的 artifacts
  │
  └─ for each turn in turns:
      _turn_to_wire(turn, ws, skills_dir)
      → {'turn_key': 'turn:0', 'artifacts': [...], 'references': [...]}
```

### turns 排序

**`_turn_sort_key()`（line 1725-1729）**：按 `turn:N` 中的 N 数值升序排列；非 `turn:` 格式的 key 放在最后。

### 前端如何利用 `manifest.turns[]` 区分各轮产物

#### 步骤 1：Chat UI 构建时为每个 turn 打上 `data-turn-key`

**`ui.js:8219`**：聊天消息渲染时，每个 assistant turn 的 DOM 元素被标记：

```javascript
// ui.js:8218-8219
if (currentAssistantTurnUserRawIdx !== undefined && currentAssistantTurnUserRawIdx >= 0) {
    currentAssistantTurn.dataset.turnKey = `turn:${currentAssistantTurnUserRawIdx}`;
}
```

`currentAssistantTurnUserRawIdx` 是该 assistant 回复对应的 user 消息在 raw 消息数组中的索引，和后端 `_message_turns()` 使用的索引一致（来自同一数据源）。

**DOM 产出示例**：
```html
<div class="assistant-turn" data-turn-key="turn:0">  ← 第 1 轮
  <div class="assistant-turn-blocks">...</div>
</div>
<div class="assistant-turn" data-turn-key="turn:5">  ← 第 2 轮
  <div class="assistant-turn-blocks">...</div>
</div>
```

#### 步骤 2：加载 manifest 后刷新聊天区产物

**`workspace.js:156-184`**：

```javascript
async function loadSessionManifest() {
    const data = await api(`/api/session/manifest?session_id=${sid}`);
    _sessionManifest = data && data.manifest ? data.manifest : null;
    renderSessionInspector();      // 侧栏 Artifacts/Refs tab
    refreshTurnArtifactsInChat();  // 聊天区 per-turn chips
}
```

#### 步骤 3：遍历 DOM 匹配 turn_key 并渲染

**`workspace.js:358-374`**：

```javascript
function refreshTurnArtifactsInChat() {
    const inner = document.querySelector('.messages-inner');
    inner.querySelectorAll('.assistant-turn[data-turn-key]').forEach(turn => {
        if (turn.id === 'liveAssistantTurn') return;  // 跳过正在流式的轮次
        const key = turn.dataset.turnKey;               // "turn:0"
        const blocks = _assistantTurnBlocks(turn);
        // 创建 .turn-artifacts 容器
        let host = turn.querySelector('.turn-artifacts');
        if (!host) {
            host = document.createElement('div');
            host.className = 'turn-artifacts';
            blocks.appendChild(host);
        }
        renderTurnArtifacts(key, host);  // 按 key 渲染产物 chips
    });
}
```

**关键**：`turn.dataset.turnKey`（来自 ui.js 设置的 `data-turn-key` 属性）必须和 manifest 返回的 `turn_key` 一致。

#### 步骤 4：按 turn_key 查找产物并渲染

**`workspace.js:329-351`**：

```javascript
function getTurnArtifacts(turnKey) {
    const manifest = _manifestForActiveSession();
    const turn = manifest.turns.find(row => row && row.turn_key === turnKey);
    return turn && Array.isArray(turn.artifacts) ? turn.artifacts : [];
}

function renderTurnArtifacts(turnKey, root) {
    const items = getTurnArtifacts(turnKey);  // manifest.turns[N].artifacts
    if (!items.length) { root.remove(); return; }
    root.innerHTML = items.map(item => `
        <button class="turn-artifact-chip" data-path="${item.path}">
            ${item.path}
            <span>${item.source_tool} · ${item.profile}</span>
        </button>
    `).join('');
}
```

#### 步骤 5：SSE 流式增量合并

**`workspace.js:271-289`**：SSE 的 `manifest_delta` 通过 `_mergeManifestTurns()` 合并：

```javascript
function _mergeManifestTurns(existingTurns, incomingTurns) {
    const byKey = new Map();
    const add = (turn) => {
        const key = String(turn.turn_key || '').trim();
        const current = byKey.get(key) || { turn_key: key, artifacts: [], references: [] };
        byKey.set(key, {
            turn_key: key,
            artifacts: _mergeManifestRows(current.artifacts, turn.artifacts),
            references: _mergeManifestRows(current.references, turn.references),
        });
    };
    (existingTurns || []).forEach(add);
    (incomingTurns || []).forEach(add);
    return [...byKey.values()].sort(/* 按 turn:N 排序 */);
}
```

#### 完整时序图

```
对话流式进行中:
  ┌────────────────────────────────────────────────────┐
  │ ui.js 渲染 assistant-turn DOM                       │
  │ → <div class="assistant-turn" data-turn-key="turn:5"> │
  │ (此时不渲染产物 chips，等 done 后统一刷新)             │
  └────────────────────────────────────────────────────┘
                        │
                        ▼ done 事件触发
  ┌────────────────────────────────────────────────────┐
  │ scheduleRefreshSessionManifest()  (120ms 防抖)       │
  │   → loadSessionManifest()                           │
  │     → GET /api/session/manifest                     │
  │     → _sessionManifest = response                   │
  │     → refreshTurnArtifactsInChat()                  │
  │        ├─ 遍历 [data-turn-key="turn:0"]             │
  │        │   → getTurnArtifacts("turn:0")             │
  │        │   → manifest.turns.find(turn:0)             │
  │        │   → 渲染 chips                              │
  │        ├─ 遍历 [data-turn-key="turn:5"]             │
  │        └─ ...                                        │
  └────────────────────────────────────────────────────┘
```

#### 关键对齐点

| 层级 | turn_key 格式 | 来源 |
|------|-------------|------|
| 后端 `_message_turns()` | `"turn:<user_msg_idx>"` | user 消息在 `messages[]` 中的索引 |
| 前端 `ui.js:8219` | `"turn:${userRawIdx}"` | user 消息在 raw 消息流中的索引 |
| manifest API 响应 | `"turn:0"` | 同后端 |
| SSE `manifest_delta` | `"turn:0"` | 同后端 |
| 前端 DOM 匹配 | `turn.dataset.turnKey === turn_key` | 字符串全等比较 |

**对齐前提**：后端 `messages[]` 和前端 raw 消息流的索引一致（来自同一数据源，自然一致）。
