# 知识库 MCP 会话轮次引用集成方案

- 状态：已实施
- 范围：`ithink_kb_mcp` 的知识库检索结果在 Session Inspector 中按会话轮次展示；引用从会话工具事件派生，不额外持久化
- 关联：[`session-inspector-manifest.md`](../architecture/session-inspector-manifest.md)、[`session-manifest-artifacts.md`](../architecture/session-manifest-artifacts.md)、[`turn-key-backend.md`](../architecture/turn-key-backend.md)、[`integration-knowledge-base-api.md`](../integration/integration-knowledge-base-api.md)

## 1. 目标与边界

用户希望在会话中能追溯「本轮回答实际检索并使用了哪些知识库文档」。每个成功完成的知识库检索应在其发起的 user turn 下产生可读的引用项；同一来源在同一轮只显示一次。

本方案只记录检索结果中实际返回的文档来源，不把搜索命中、目录枚举、助手正文里提到的文件，或未完成/失败/取消的调用当作引用。这与 Session Inspector 的既有定义一致：Manifest 是活动的派生索引，不是 transcript、执行日志或工作区文件清单。

本方案不做以下事情：

- 不额外将完整 MCP 原始响应或查询原文写入 sidecar、Manifest 或前端状态，也不向客户端发送下游绝对 `metadata.source` 路径。
- 不推断「助手一定在最终答案中引用了哪一条检索结果」；记录的是本轮实际获得的检索来源。
- 不改变 MCP 管理 API、MCP 服务器配置或知识库服务的检索契约。
- 不把知识库文档伪装成 workspace artifact，也不把它加入上传/下载文件列表。

## 2. 已知工具契约

根据已捕获的成功 `tool.completed` 事件，当前 `ithink_kb_mcp` 至少有以下两个工具。两者的入参和结果包装并不相同，接入层必须做显式规范化。

| MCP 工具 | 用途 | 已观察到的输入 | 已观察到的每条结果 |
| --- | --- | --- | --- |
| `searchKnowledgeBaseDocuments` | 在一个知识库中检索 | `kbName`、`query`、`topK`、`scoreThreshold` | `page_content`、`metadata.source`、`type`、`score` |
| `searchKnowledgeBaseDocumentsAcross` | 在多个知识库中检索 | `kbNames`、`query`、`topK`、`scoreThreshold` | `page_content`、`metadata.kbName`、`metadata.fileName` |

两个工具的 `result` 都是被外层 JSON 再包装的 JSON 字符串。`kbNames`、`topK`、`scoreThreshold` 在观测事件中也是字符串。因此不能依赖 MCP 展示卡的格式，必须从已结算工具事件的结构化 args/result 做受限解析。

单库工具的 `metadata.source` 是下游文件系统绝对路径，不能向浏览器或持久化索引暴露。跨库工具没有 `source`，但提供了足以构造展示引用的 `metadata.kbName` 与 `metadata.fileName`。规范化引用统一为这两个字段；单库调用需要从受控的路径尾部取文件名，无法可靠得到库名时不生成引用。

### 2.1 已捕获的完整工具事件结构

方案中的解析输入是完整的已结算 `tool.completed` 事件，而不是聊天卡片的截断展示。事件外层字段保持 Agent 当前命名：`event_type`、`name`、`preview`、`args`、`tid`、`is_error`；`preview` 本身是 JSON 字符串，解包后其中的 `result` 仍是 JSON 字符串。下列示例保留完整字段和两种工具的实际参数/结果形状，`page_content` 仅用占位符表示，避免将检索正文和私有路径复制进设计文档。

```json
{
  "event_type": "tool.completed",
  "name": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
  "preview": "{\\\"result\\\":\\\"[{\\\\\\\"page_content\\\\\\\":\\\\\\\"<检索片段>\\\\\\\",\\\\\\\"metadata\\\\\\\":{\\\\\\\"source\\\\\\\":\\\\\\\"/data/.../knowledge_base/share49/content/电力市场运行基本规则.docx\\\\\\\"},\\\\\\\"type\\\\\\\":\\\\\\\"Document\\\\\\\",\\\\\\\"score\\\\\\\":0.5659806728363037}]\\\"}",
  "args": {
    "kbName": "share49",
    "query": "电力交易 法规 规则",
    "scoreThreshold": "1",
    "topK": "5"
  },
  "tid": "call_01_waVoFjsFgv4NXNjduRgG7219",
  "is_error": false
}
```

```json
{
  "event_type": "tool.completed",
  "name": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
  "preview": "{\\\"result\\\":\\\"[{\\\\\\\"page_content\\\\\\\":\\\\\\\"<检索片段>\\\\\\\",\\\\\\\"metadata\\\\\\\":{\\\\\\\"kbName\\\\\\\":\\\\\\\"share49\\\\\\\",\\\\\\\"fileName\\\\\\\":\\\\\\\"电力市场运行基本规则.docx\\\\\\\"}}]\\\"}",
  "args": {
    "kbNames": "['personal142', 'personal162', 'share138', 'share20', 'share49', 'share158', 'share171']",
    "query": "电力交易法",
    "topK": "10",
    "scoreThreshold": "1"
  },
  "tid": "call_00_JYHhyl66bmamRZqKHmsK5225",
  "is_error": false
}
```

服务端会解包 `preview`，将其中每条结果归约为一条引用，而不是把外层 JSON 字符串原样下发或持久化。历史 transcript 中如由 Agent 写成 `<untrusted_tool_result>...` 安全包装，只有包装正文中唯一完整的 `{"result": ...}` JSON 对象才会解包；错误、多个候选或自然语言文本一律跳过。命中同一文档的结果会将 `page_content` 聚合为 `metadata.page_content` 列表；`type` 和 `score` 不进入引用。单库结果的私有 `metadata.source` 不会透传，而是与调用 `args.kbName` 一起规范化为公开的 `metadata.kbName/fileName`。

## 3. 总体设计

```text
Agent tool_complete
  -> 既有 _emit_manifest_delta()
  -> 知识库结果规范化（integration 层）
  -> 既有 manifest_delta SSE
  -> Session Inspector 的该 turn References 区

会话结算/刷新
  -> build_session_manifest() 按既有 turn 边界重新派生同一引用
```

### 3.1 模块边界

Fork 特有解析器位于既有知识库边界 `integration/knowledge_base/turn_references.py`，避免把规则堆进 `api/streaming.py` 或 `api/session_manifest.py`：

| 模块 | 职责 |
| --- | --- |
| `turn_references.py` | 识别受支持工具、受限解包 `args/result`、产生和合并规范化候选，并投影 manifest `references` wire；不访问网络或本地文件。 |
| `static/workspace.js` | 只消费 manifest wire 字段、合并 SSE delta 并渲染，不解析工具原始结果。 |

`api/session_manifest.py` 只保留两个薄钩子：`extract_manifest_delta_from_tool_event()` 为实时流调用 `extract_references()`，`_extract_manifest_records()` 为结算/刷新调用同一提取函数。`api/streaming.py` 已有 `_emit_manifest_delta()`：它负责构造 `ToolEvent`、调用前者并发送 `manifest_delta`，本身不承载知识库解析规则。全程不增加事件类型、长驻状态或新的 Gateway 分支；`manifest_delta` 已由前端的 `HermesSessionInspector.applyDelta()` 消费。

### 3.2 工具识别与受限解析

不增加可配置的 server/tool 映射。集成层只精确识别以下两个完整工具标识：

```text
mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments
mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross
```

若事件已分离 `server` 与 `name`，则分别精确比较 `ithink_kb_mcp` 和这两个工具名；若只保留合成名，则直接比较完整字符串。不得以 `kb`、`knowledge` 等子串猜测任意第三方 MCP 是知识库检索。未知服务器或工具不产生引用。

规范化器只接受以下状态：

1. 工具事件已经由 `_collect_tool_events()` 与 completed result 配对；
2. 执行状态明确成功，且没有 `is_error`、失败标记或非零退出状态；
3. 结果可在大小上限内解包为对象数组；
4. 每条记录含可公开的 `kbName + fileName` 身份。

解析的大小限制建议为：外层 result 最大 1 MiB、最多 50 条结果。通过检查后，每条接受的结果将完整保留其 `page_content` 并聚合到对应 `metadata.page_content[]`，不在引用层截断；单次 `references` 最大 1 MiB。超过任一限制、JSON 形状异常、缺失身份或路径无法安全归一化时跳过该条并记诊断计数，不能降级为保存原始文本。

单库调用须优先从受信任的 MCP 返回中取得 `metadata.kbName`/`metadata.fileName`；当前观察到的形状没有这两个字段，过渡实现可从 `metadata.source` 的 basename 得到 `file_name`，但**不能**把 `/data/.../knowledge_base/<kb>/...` 这种私有路径反推后暴露。若 `args.kbName` 是规范化字符串且结果 basename 非空，则使用该入参作为 `kb_name`；否则跳过记录。

### 3.3 规范化引用与去重

每条引用将工具调用来源与解包后的工具结果放在一起。`source` 是调用来源列表，每项只保留完整 MCP 工具名与调用 ID；所有文档展示数据统一放在 `metadata`：`page_content` 沿用 MCP 字段名但改为片段列表，`kbName/fileName` 是文档身份。不会重复下发外层 `preview` 字符串、`result` 数组、`type`、`score`、query 或其它调用参数。

```json
{
  "kind": "knowledge_base_document",
  "source": [
    {
      "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
      "tid": "call_01_abc"
    },
    {
      "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
      "tid": "call_02_def"
    }
  ],
  "metadata": {
    "kbName": "share49",
    "fileName": "电力市场运行基本规则.docx",
    "page_content": [
      "第一章 总则……",
      "第一章 适用范围……"
    ]
  }
}
```

资源规范键为：

```text
kbref:v1:<percent-encoded kbName>:<percent-encoded fileName>
```

同一 `turn_key + kbName + fileName` 只留一条引用，不区分它来自 `searchKnowledgeBaseDocuments` 还是 `searchKnowledgeBaseDocumentsAcross`。相同文档的多个命中片段按首次出现顺序去重后追加至 `metadata.page_content[]`；每个产生该文档的调用均按首次出现顺序加入 `source[]`，以 `(tool, tid)` 去重。仅当规范化后的 `kbName` 与 `fileName` 都完全相等时才合并，禁止按相似文件名、正文相似度或路径片段模糊匹配。不同轮次可以分别引用同一文档；压缩后的子会话沿用既有 lineage transcript 合并规则后，仍能看到各轮记录。

## 4. 实时派生与生命周期

本功能不新增数据库表、会话 sidecar 字段或长驻内存状态。引用的权威来源是已持久化的 transcript 与 `session.tool_calls` 中的成功 completed MCP 事件；Session Manifest 已经在读取时将这些来源规范化为 `ToolEvent`。

实时路径复用当前聊天 SSE 的 `manifest_delta`，不新增 `knowledge_base_reference` 之类的事件：

```text
on_tool_complete()
  -> _emit_manifest_delta(name, args, full_result, status="completed")
  -> extract_manifest_delta_from_tool_event() 解析结果
  -> integration.knowledge_base.turn_references.extract_references()
  -> put("manifest_delta", delta)
  -> HermesSessionInspector.applyDelta(delta)
```

`on_tool_start` 与失败完成事件不产生知识库引用。较旧的 Agent 回调若只能提供截断 `preview`，解析失败时实时 delta 不产生引用；待会话落盘后，结算路径从完整 tool message / `session.tool_calls` 重新派生。这是 fail-closed 的降级，不尝试从截断文本猜测文档。

结算、刷新和重连使用同一提取器：

```text
build_session_manifest()
  -> _collect_tool_events(messages, session.tool_calls)
  -> integration.knowledge_base.turn_references.extract_references(...)
  -> _turn_key_for_event() 归属到已有 turn
  -> manifest.references / turns[].references
```

实时 delta 同时由现有 `STREAM_LIVE_MANIFEST` 保存到当前 stream；`GET /api/session/manifest` 会合并它，以覆盖 SSE 丢帧期间的活动流读取。该接口仍是严格只读操作：不回填、不网络重检索、不写数据库。会话删除、清空或按 turn 截断后，源事件不再属于读取范围，引用会自然消失，无须额外清理。重放场景在同一次构建中按 `turn_key + kbName + fileName` 去重，不产生重复引用。

### 4.1 历史会话读取

需要同步调整 `GET /api/session/manifest` 的**构建实现**，但不新增路径、查询参数或响应外壳。`build_session_manifest()` 已经调用 `_collect_tool_events()`，历史会话的完成工具结果可从 transcript 与 `session.tool_calls` 得到；在 `_extract_manifest_records()` 中调用同一知识库提取器后，结果分别进入顶层 `references` 与对应的 `turns[].references`。

现有 `_rows_to_wire(..., collection="references")` 只投影 Skill 引用。实施时必须让它把 Skill 与知识库都投影为统一的 `kind + source[] + metadata` 结构；知识库行的 `metadata` 额外携带从 `preview` 解包后的 `page_content[]`。因此本次是既有 Manifest 响应中 `references` 行 schema 的契约调整，须同步更新 `docs/api/session-manifest-api.md`、前端渲染与相关历史会话测试。

当前既有的引用行是成功 `skill_view` 产生的 canonical Skill，实际 wire 形状如下：

```json
{
  "path": "research-skill",
  "preview": "skill",
  "source_tool": "skill_view"
}
```

统一后，同一条 Skill 引用投影为：

```json
{
  "kind": "skill",
  "source": [
    {
      "tool": "skill_view",
      "tid": "call_02_def"
    }
  ],
  "metadata": {
    "path": "research-skill"
  }
}
```

### 4.2 SSE 与 Manifest 示例

浏览器不解析原始 `tool_complete` 结果。服务端在同一次工具完成回调中解包 `preview` 并发送既有 `manifest_delta` SSE。知识库来源属于单篇引用，统一放在 `references[].source`，而不是 delta 顶层；一个工具调用返回多篇文档时，各文档行携带包含该调用的 `source[]`，以及各自 `metadata.page_content[]` 的解析片段。

```text
event: manifest_delta
data: {
  "version": 1,
  "session_id": "session_123",
  "stream_id": "stream_456",
  "turn_key": "turn:8",
  "artifacts": [],
  "references": [
    {
      "kind": "knowledge_base_document",
      "source": [
        {
          "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
          "tid": "call_01_abc"
        }
      ],
      "metadata": {
        "kbName": "share49",
        "fileName": "电力市场运行基本规则.docx",
        "page_content": ["第一章 总则……"]
      }
    }
  ]
}
```

查看历史、刷新页面或 SSE 重连后，客户端继续使用原接口：

```http
GET /api/session/manifest?session_id=session_123
```

```json
{
  "manifest": {
    "todos": {"items": []},
    "artifacts": [],
    "references": [
      {
        "kind": "skill",
        "source": [
          {
            "tool": "skill_view",
            "tid": "call_02_def"
          }
        ],
        "metadata": {
          "path": "research-skill"
        }
      },
      {
        "kind": "knowledge_base_document",
        "source": [
          {
            "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
            "tid": "call_01_abc"
          },
          {
            "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
            "tid": "call_02_def"
          }
        ],
        "metadata": {
          "kbName": "share49",
          "fileName": "电力市场运行基本规则.docx",
          "page_content": ["第一章 总则……", "第一章 适用范围……"]
        }
      }
    ],
    "turns": [
      {
        "turn_key": "turn:8",
        "artifacts": [],
        "references": [
          {
            "kind": "skill",
            "source": [
              {
                "tool": "skill_view",
                "tid": "call_02_def"
              }
            ],
            "metadata": {
              "path": "research-skill"
            }
          },
          {
            "kind": "knowledge_base_document",
            "source": [
              {
                "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
                "tid": "call_01_abc"
              },
              {
                "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
                "tid": "call_02_def"
              }
            ],
            "metadata": {
              "kbName": "share49",
              "fileName": "电力市场运行基本规则.docx",
              "page_content": ["第一章 总则……", "第一章 适用范围……"]
            }
          }
        ]
      }
    ],
    "diagnostics": {
      "missing_turn_key_message_indices": []
    }
  },
  "manifest_source": "db"
}
```

同一轮多个文档时，`references` 添加多个解析后的引用对象；同一文档在同一轮只出现一次，即使两个知识库工具都返回它。Skill 与知识库行使用相同的 `kind + source[] + metadata` 结构，渲染器只根据 `kind` 选择资源预览或文档展示方式；知识库行正文使用 `metadata.page_content[]`，必须按纯文本折叠展示。

## 5. Manifest 与前端投影

将现有 `references` 扩展为统一的资源引用行：`kind` 是资源类型，`source[]` 是产生该行的工具调用，`metadata` 是资源身份和资源展示数据。知识库行在 `metadata.page_content[]` 带上从 `preview` 解包的结果正文；Skill 不携带该字段：

```json
{
  "kind": "knowledge_base_document",
  "source": [
    {
      "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
      "tid": "call_01_abc"
    }
  ],
  "metadata": {
    "kbName": "share49",
    "fileName": "电力市场运行基本规则.docx",
    "page_content": ["第一章 总则……"]
  }
}
```

顶层 `manifest.references` 是按资源身份去重的汇总：Skill 使用 `metadata.path`，知识库使用 `(metadata.kbName, metadata.fileName)`；`turns[].references` 保留各自轮次的引用。合并同一知识库文档时，`source[]` 按 `(tool, tid)` 合并，`metadata.page_content[]` 按文本合并；这一规则同时应用于实时 `applyDelta()` 与服务端 Manifest 构建。排序先按 `kind`，再按对应资源身份，避免顺序跳变。

Session Inspector 在每轮的 References 区展示文档名、知识库名和折叠的 `metadata.page_content[]`。无引用时不新增空卡片；旧 Skill 引用继续使用原有入口。

对于已配置的知识库 BFF，可在用户主动点击文档时调用既有 `POST /api/integration/knowledge_base/show_pdf`，传入 `kbName` 和 `fileName`。该操作是新的按需读取，不得在 manifest 构建或初始渲染时预取；下游的访问控制与失败结果原样保持。未配置 BFF、下游无预览能力或文档已删除时，只显示不可点击的引用元数据，不报告为打开成功。

## 6. 兼容性、隐私与安全

- 旧会话只要仍保留可识别的成功工具事件，就会在读取 manifest 时产生引用；缺少完成事件、结果或公开文档身份时保持为空，不做猜测或回填。
- 这是 references row 的 wire 形状调整。WebUI 前后端应在同一版本同步发布；若存在独立外部消费者，先让其兼容旧 `path/preview/source_tool` 与新 `kind/source/metadata` 两种行，再切换服务端输出。
- 不在会话、Manifest 或额外状态中保存 query，避免将用户问题、账号上下文或敏感筛选语句扩散出去。诊断只计数，不记录外层原始响应。
- `metadata.page_content[]` 会随 SSE 与 Manifest 下发给当前会话的浏览器；实现前须确认该检索内容可按当前 WebUI 会话权限展示。仍不向客户端发送 `metadata.source`、下游路径、MCP headers、账号或 UUID。
- `metadata.kbName` 与 `metadata.fileName` 作为不可信远端文本，在 JSON 和 DOM 中一律按文本处理；禁止插入 HTML。
- `show_pdf` 的点击请求不携带 MCP 凭据，继续使用 WebUI 的 cookie session、CSRF 与既有 integration handler。
- Manifest 只从当前已解析会话的 transcript 与 `session.tool_calls` 提取，不读取其他会话或 profile 的数据；同名知识库或文件名不会跨会话聚合。

## 7. 验证计划

单元测试位于 `integration/tests/knowledge_base/test_turn_references.py`，现有 manifest/stream 测试补充覆盖。

| 场景 | 预期 |
| --- | --- |
| 单库成功结果 | 从 `args.kbName` + `metadata.source` basename 生成一条当前 turn 引用，在 `metadata.page_content[]` 保留完整片段，不泄露绝对路径。 |
| 跨库成功结果 | 依据每条 `metadata.kbName/fileName` 生成多条引用，将同文档片段聚合到各自 `metadata.page_content[]`。 |
| 引用 wire | Skill 与知识库引用均为 `kind + source[] + metadata`；知识库行通过 `metadata.page_content[]` 携带解包后的片段。 |
| 两个知识库工具命中同一文档 | 同一 turn 仅一条引用，两个 `(tool, tid)` 进入 `source[]`，片段聚合进 `metadata.page_content[]`。 |
| 同一调用重放、同轮重复命中 | 单次 manifest 构建的 wire 中只有一条引用，`source[]` 与片段列表均不重复。 |
| 多轮同一文档 | 顶层一条汇总，各轮各有一条。 |
| Gateway 与本地 Agent | 两种会话落盘的工具事件均使用同一 manifest 规范化逻辑。 |
| 实时 SSE | 结构化 `tool_complete.preview` 解包后的结果经既有 `manifest_delta` 在同一轮即时显示；不新增 SSE 事件类型。 |
| 截断实时结果 | 旧回调的 `preview` 不能完整解析时不推测；会话结算后以完整已落盘事件重新派生。 |
| 完整内容与大小边界 | 已接受结果的 `page_content` 与解包结果完全一致；超过 1 MiB 或 50 条时 fail-closed，不产生部分或截断引用。 |
| 失败、取消、孤立 result、未知 turn、畸形/超大 JSON | 不产生引用，不向最新轮次错误归属。 |
| 压缩、重连、服务重启 | 同一 lineage/profile/turn 的引用仍可读取；重放不重复。 |
| 删除、清空、截断和 profile 隔离 | 源事件不再可读时引用自然消失；其他会话/profile 不受影响。 |
| 前端窄屏与移动端 | References 可折叠、长文档名换行且无横向溢出；预览失败仍保留引用元数据。 |

建议验证命令：

```bash
./scripts/test.sh integration/tests/knowledge_base tests/test_session_manifest.py tests/test_session_manifest_contract.py -q
```

实施 PR 还应以隔离的 `HERMES_HOME` 与 `HERMES_WEBUI_STATE_DIR` 做手动验证，构造单库、跨库、失败和取消四种会话；不得使用或打印真实知识库响应与凭据。

## 8. 实施拆分

1. 已建立 `integration/knowledge_base/turn_references.py` 的纯规范化器和测试，固定两个已知工具的差异、大小限制与隐私过滤。
2. 已在 `api/session_manifest.py` 的实时 delta 与结算提取两个薄钩子调用同一规范化器，复用既有 `manifest_delta`。
3. 已扩展 manifest wire 与 Session Inspector 渲染；知识库引用只读展示文件名、知识库名与可展开片段，不新增预览调用。
4. `show_pdf` 的权限和文档类型行为尚未纳入本功能；引用保持只读元数据展示。

每一步均可独立回滚。步骤 1--2 不增加任何持久化状态，也不改变聊天执行或 MCP 调用；显示层可通过 feature flag 关闭。

## 9. 尚待确认的契约

- 单库工具能否稳定返回 `kbName` 和 `fileName`，而不是仅返回私有 `source` 路径；若不能，应由 MCP 服务补充公开元数据，而不是扩大路径解析规则。
- 各 Agent 版本与 Gateway 的 completed-event 配对字段是否都能被 `_collect_tool_events()` 识别；不能配对时该事件不产生引用。
- `show_pdf` 是否覆盖所有检索返回的文档类型，以及它是否会对无权限用户返回可区分的状态；确认前不把引用项做成可点击链接。
