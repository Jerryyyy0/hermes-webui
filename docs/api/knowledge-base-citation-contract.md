# IThink 知识库最终回答 Chunk 级引用契约（提案）

> **状态：Proposed**
>
> **适用范围：** 通过 `/api/chat/stream` 消费聊天结果，并通过
> `/api/session/manifest` 读取会话 References 的外部前端。
>
> 本文只定义后端与外部前端之间的 wire contract，不规定 WebUI 内部页面如何渲染角标。

## 1. 问题与目标

当前 Session Manifest 已会从以下两个成功完成的 MCP 工具结果中提取知识库文档：

- `mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments`
- `mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross`

它们进入 `manifest.references`，但其语义是“本轮检索命中”，不是“最终回答实际采用”。若直接把所有
`references[]` 渲染为回答角标，会把未被模型使用、仅作为候选的文档也错误标记为引用。

本提案定义第二层、可验证的 `message.citations[]`：

```text
工具成功检索 -> Manifest candidate reference
模型显式声明采用 -> 服务端验证并结算 message.citations
message.citations[].reference_id + message.citations[].chunk_ids[0]
  -> Manifest references[].id + metadata.chunks[].id
```

目标：

1. 最终 assistant message 的正文包含可直接由 Markdown+HTML 渲染器显示的引用角标，例如
   `<sup data-c="1">[1]</sup>`。
2. 每个角标可用 `reference_id + chunk_ids[0]` 精确映射到 Manifest 中一个知识库文档的一个 chunk。
3. 只有本轮、两个受支持工具、成功 completed 的真实检索结果可以被引用。
4. 引用必须在最终回答结算时被服务端校验；不能由外部前端、模型自由文本或文件名猜测。
5. 重连、`done` 重放、刷新会话和 Manifest 重建后仍能得到相同的引用关联。

非目标：

- 不要求服务端通过字符串相似度或 embedding 推断“这句话像不像某个 chunk”。那不能证明模型实际使用了该结果。
- 不将每个检索命中自动变成角标。
- 不在 token 流阶段发送最终角标；最终引用以本轮 `done` 事件为准。
- 不变更两个 MCP 工具本身的检索排序、分数和业务参数。

## 2. 术语和身份

| 名称 | 含义 |
| --- | --- |
| Candidate reference | 某个受支持 MCP 工具成功 completed 后，从结果中提取的知识库文档及其合法 chunks；它只存在于当前 stream 的候选注册表，不等于公开 `manifest.references[]`。 |
| Citation candidate token | 服务端为单个文档 chunk 生成、只在当前 turn 有效的 opaque token，供 Agent 在最终回答中声明采用该 chunk。 |
| Citation | 服务端验证后的一个最终回答角标实例。一个 Citation 精确对应一个 document chunk；回答使用多个 chunks 时产生多个 Citation。 |
| `reference_id` | 文档级稳定标识，等于 Manifest 内该文档的 `references[].id`。 |
| `chunk_id` | 文档内命中片段的稳定标识。每个 Citation 必须精确绑定一个 `chunk_id`。 |

### 2.1 `reference_id` 和 `chunk_id`

服务器必须在同一处计算 ID，禁止由外部前端自行拼接。文档身份优先使用下游返回的不可变
`document_id`；无不可变 ID 时，才使用规范化名称作为降级身份：

```text
authority_namespace = canonical(kb_service_instance + tenant/account namespace)

if immutable document_id exists and authority_namespace is trustworthy:
    document_key = "immutable\\0" + authority_namespace + "\\0" + document_id
elif authority_namespace is trustworthy:
    document_key = "named\\0" + authority_namespace + "\\0" + kbName + "\\0" + fileName
else:
    local_document_identity = document_id if immutable document_id exists else kbName + "\\0" + fileName
    document_key = "session\\0" + profile + "\\0" + session_id + "\\0" + local_document_identity

reference_id = "kbdoc:v1:" + base64url(SHA-256(utf8(document_key)))

if immutable chunk_id exists:
    chunk_key = "immutable\\0" + reference_id + "\\0" + chunk_id
else:
    # source_call_key = canonical(tool + "\\0" + tid)
    # row_index/chunk_index are the original positions in this one result.
    chunk_key = (
        "call-scoped\\0" + source_call_key + "\\0" + str(row_index)
        + "\\0" + str(chunk_index) + "\\0" + page_content
    )

chunk_id = "kbchunk:v1:" + base64url(SHA-256(utf8(chunk_key)))
```

完整 SHA-256 摘要不得截断。`authority_namespace` 必须来自已认证的下游响应或部署配置，不能取模型文本、用户请求
参数或 MCP 返回的自由文本字段。只有处于同一权威命名空间的相同不可变 `document_id`，或同一权威命名空间内
降级后的相同 `(kbName, fileName)`，才能跨两个受支持工具合并为同一个 `reference_id`；`source[]` 继续按既有规则
保留全部真实 `tool + tid` 来源。跨工具合并 document 不等于跨工具合并 chunk；chunk 只有在可信不可变
`chunk_id` 相同且正文校验一致时才能合并，否则保留各自 call-scoped `chunk_id`。

如果下游既不返回可信不可变 ID，也不能确定租户/账户命名空间，必须使用上述 session-scoped 降级，不能生成看似
全局稳定的同名文档 ID。该 ID 只保证在当前 `profile + session_id` 内稳定。Candidate、Settlement、Manifest live/
rebuild cache 的完整 key 均至少包含 `profile + session_id + reference_id`，不得只用 `reference_id` 做跨会话缓存键。

`kbName`、`fileName` 和 `page_content` 都取已经通过现有知识库结果解析和大小限制后的规范化值。
禁止将私有 `metadata.source` 路径、工具请求参数或模型输出的文件名用于生成身份。

Chunk 规范化必须保留下游提供的可信不可变 `chunk_id`；不能在 `_chunk()` 或合并阶段丢弃它。没有可信不可变
ID 时，必须保留该 chunk 在**单次工具调用结果**中的 `row_index + chunk_index`，并将规范化的
`tool + tid` 作为 `source_call_key` 纳入 fallback identity。同一 Document 下即使两个 chunks 的
`page_content` 完全相同，也必须生成两个不同的 `chunk_id`。没有不可变 chunk ID 时，不得仅凭相同正文断言
两个不同工具调用返回的是同一个 chunk；它们应保持为不同的 call-scoped candidates。

`v1` 是本协议首次定义、首次实现的 document/chunk ID 格式；当前不存在需要兼容或迁移的旧版 ID。未来若因
identity key 或哈希算法发生不兼容变化，才新增 `v2` 前缀；实现不得把不同版本的 ID 混用或静默重解释。本文后续
带省略号的 ID 只表示 wire 形状，不是固定测试向量。

## 3. 数据模型

### 3.1 Manifest reference 扩展

`GET /api/session/manifest` 中的知识库 document 增加下列字段；既有字段保持不变。
知识库 document 不通过工具 completed 阶段的 SSE `manifest_delta.references[]` 实时发送，见 §4.1。

```json
{
  "id": "kbdoc:v1:Fb8...",
  "kind": "knowledge_base_document",
  "source": [
    {
      "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
      "tid": "call_01"
    }
  ],
  "metadata": {
    "kbName": "share49",
    "fileName": "电力市场运行基本规则.docx",
    "chunks": [
      {
        "id": "kbchunk:v1:Q3c...",
        "page_content": "第一章 总则……",
        "score": 0.82
      }
    ]
  }
}
```

`id` 是文档级 ID；`metadata.chunks[].id` 是片段级 ID。一个候选文档只有在成功 completed 的实际
工具结果可被严格解析时才进入 Manifest，沿用现有 fail-closed 规则。

同一 document 的 `metadata.chunks[]` 按有效数值 `score` 降序输出；相同 score 保持工具结果中的原始顺序，缺失、
非数值或非有限 score 排在最后。provider-facing 工具结果也必须在每个 Document 内按同一规则排列 chunks，并在排序
前保留各 chunk 已绑定的 `_cite`，确保模型所见顺序和 Manifest 详情一致。此排序不改变 candidate 的原始
`row_index + chunk_index` 身份，也不改变最终 `citations[].ordinal` 的正文出现顺序。

`references[]` 仍然表示“检索候选”，不因增加 `id` 而改成“已在回答中使用”。外部前端需要展示最终
引用时，必须以 `message.citations[]` 为准。

### 3.2 最终 assistant message 的 `citations[]`

本轮最后一条真实 `role=assistant` message 在成功结算后可包含：

这里复用 WebUI 现有的 `id` 字段：它是 `_assign_stable_message_ids()` 为消息分配的会话内递增正整数。本文不新增
`message_id` 字段；跨会话定位一条消息时使用 `session_id + id`，不能把 `id` 当成全局唯一标识。

```json
{
  "role": "assistant",
  "id": 42,
  "content": "根据现有资料，1000kV GIS 断路器的额定电流没有被直接给出。<sup data-c=\"1\">[1]</sup>",
  "citations": [
    {
      "citation_id": "kbcite:v1:7f2a...",
      "ordinal": 1,
      "reference_id": "kbdoc:v1:Fb8...",
      "chunk_ids": ["kbchunk:v1:Q3c..."],
      "source": {
        "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
        "tid": "call_01"
      }
    }
  ]
}
```

字段约束：

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `citation_id` | string | 本 message 内唯一；由 host settlement 生成不可预测的 opaque ID（至少 128-bit 随机量）并随 Citation 持久化。客户端不得计算；重放直接复用持久化值。 |
| `ordinal` | positive integer | 只在该 assistant message 内编号，从 `1` 连续递增。一个唯一 `(reference_id, chunk_ids[0])` 只分配一个 ordinal；同一 chunk 出现在多个正文位置时复用该 ordinal。 |
| `reference_id` | string | 必须等于同一 turn 可见的某个 `knowledge_base_document` Manifest reference 的 `id`。 |
| `chunk_ids` | one-element string[] | 必须只包含一个属于该 `reference_id` 的 `metadata.chunks[].id`；不同 chunk 必须使用不同 token 和不同 Citation。 |
| `source` | object | 产生该 Citation 的唯一成功检索调用，固定为 `{tool, tid}`，且必须是该 reference 的 `source[]` 子集。 |

`message.citations` 为空数组或字段缺失都表示“本消息没有已验证的知识库引用”。外部前端不得依据
`content` 中看似相同的 `[1]`、`<sup>` 或文档名自行制造 Citation；只承认 `citations[]` 中列出的
`ordinal`。公开 marker 由固定公式 `<sup data-c="${ordinal}">[${ordinal}]</sup>` 推导，不作为重复字段传输。
`citation_id`、`chunk_ids` 与 `{tool, tid}` 继续对外保留，用于幂等重放、展示精确片段与追溯真实工具调用；
它们也必须与服务端私有 `_knowledge_base_citation_evidence` 一致，见 §4.4。

#### 3.2.1 公开 marker 的渲染规则

最终 `content` 是既有 Markdown 文本，允许服务端仅为 Citation 注入下列受限 inline HTML：

```html
<sup data-c="1">[1]</sup>
```

这不是模型应输出的格式。模型只输出 §4.2 的内部 token；citation settlement 负责以服务器生成的 `ordinal`
创建公开 marker。这样外部前端的 Markdown renderer 若允许 `sup` 与 `data-c`，无需把
`[^1]` 识别为某种特定脚注方言，即可直接显示上角标。

渲染安全约束如下：

- 允许的标签仅为 `sup`，允许的属性仅为 `data-c`；不得将 Citation marker 变成模型可控制的 `href`、
  `style`、事件属性或任意 HTML。
- 渲染/点击绑定时，只有当 `data-c` 是十进制正整数、能精确匹配本 message 的一个 `citations[].ordinal`，且元素
  完整序列化文本等于由该 ordinal 推导的 `<sup data-c="N">[N]</sup>` 时，才把它当成真实引用并允许打开 Manifest 详情。
- settlement 必须先转义/中和模型原始正文中的 `<sup>` HTML，再注入已验证 Citation marker；否则模型伪造的
  `<sup data-c="1">[1]</sup>` 会与真实 marker 混淆。模型正文中自行生成的 `[1]` 或 `data-c` 属性不得获得
  Citation 的交互能力。
- 若外部前端仍会保留任意 Markdown HTML，推荐在渲染前将除服务端 settlement 注入 marker 外的 raw HTML 全部转义。
- 若外部前端的 Markdown sanitiser 不能保留该标签/属性，不能退回显示 `[^N]`；应改为从 `citations[]` 解析并
  渲染自己的角标组件。该 fallback 不改变 wire contract。

### 3.3 关联查询

外部前端在获得 `message.citations[]` 后，以 `reference_id` 在当前会话 Manifest 中精确查找：

```text
message.citations[i].reference_id
    == manifest.references[j].id
```

同一 turn 的窄范围查询可先使用：

```text
manifest.turns[k].turn_key == assistant message 所属 user turn 的 _turn_key
manifest.turns[k].references[j].id == citation.reference_id
```

完整映射链如下，外部前端只需要三个公开数据面：最终 `content`、同一 message 的 `citations[]` 和完整
Manifest；服务端私有 `_knowledge_base_citation_evidence` 不参与客户端映射：

```text
content 中 <sup data-c="N">[N]</sup> 的 data-c=N
  -> 同一 message.citations[] 中 ordinal=N 的唯一 Citation
  -> Citation.reference_id
  -> manifest.references[] / manifest.turns[].references[] 中 id=reference_id 的文档
  -> Citation.chunk_ids[0]
  -> 该文档 metadata.chunks[] 中 id == chunk_ids[0] 的唯一实际片段
```

例如 `content` 中的 `<sup data-c="1">[1]</sup>` 对应 `citations[]` 中 `ordinal: 1` 的条目；该条目的
`reference_id: "kbdoc:v1:Fb8..."` 再对应 Manifest 中 `id: "kbdoc:v1:Fb8..."` 的
`knowledge_base_document`，`chunk_ids[0]: "kbchunk:v1:Q3c..."` 再对应其中同 ID 的唯一 chunk。因此客户端不需要、
也不应读取 evidence snapshot。

找不到时不得以文件名做模糊匹配。客户端可以保留角标并显示“引用详情暂不可用”，然后重新拉取
`GET /api/session/manifest`；仍找不到则按数据不一致处理并上报，不显示另一篇“相似”文档。

## 4. 服务端生成与验证流程

### 4.1 工具完成：建立候选集

每个工具 completed 事件按如下顺序处理：

1. 确认工具调用已经 completed、执行层 `is_error == false`，且下游业务响应明确表示成功；任一状态缺失、未知或失败
   都不创建 Citation candidate。
2. 用现有 `integration/knowledge_base/turn_references.py` 严格解析**完整、权威的 function result**。
   `result_snippet`、`preview`、日志摘要、SSE 展示片段或经过长度截断的副本均不能作为 candidate 来源。
3. 为每个文档和 chunk 注入 §2.1 的稳定 ID，形成当前 turn 的 candidate reference set；解析时保留下游不可变
   `chunk_id`，或保留单次调用内的 `row_index + chunk_index`。每个合法 chunk 都是一个独立的 citation candidate，
   不得在此阶段按 `page_content` 去重。
4. 仅在当前 stream worker 的 candidate registry 中保存该结果；**不得**为
   `knowledge_base_document` 发送实时 `manifest_delta`、写入 `STREAM_LIVE_MANIFEST`，也不得生成
   `message.citations`。
5. 为每个“单次成功工具调用返回的一个 document chunk”生成当前 turn 专属的 opaque citation token；同一工具结果中的
   多个 chunks 必须分别生成 token；一个 token 永远只绑定一个 chunk，不能覆盖同一 Document 的其它 chunks。没有
   可信不可变 chunk ID 时，不同工具调用的同名正文不得共用 token 或 chunk ID。

当前 `api/streaming.py` 某些路径只把 `result_snippet` 交给事件提取，`api/gateway_chat.py` 某些路径只提供
`preview`；实现本提案时必须从执行器增加一个受信任的完整结果接缝，再由同一个 integration helper 解析。旧 Agent
或 Gateway 版本若只能提供截断内容，该次调用仍可显示普通工具进度，但 Citation 功能必须 fail closed，不能根据
片段猜测文档、chunk 或 token。

这条“只保存在服务端”的规则只针对知识库 document reference。现有 `manifest_delta` 的其它类型（例如任务、
Artifact）不受影响。candidate registry 是最终验证所必需的临时状态；不发送 SSE 不代表跳过解析、ID 生成或
source/tid 绑定。

现有 `_emit_manifest_delta()` / `emit_gateway_manifest_delta()` 会先把完整 `_delta` 合并进
`STREAM_LIVE_MANIFEST`，再发送 SSE，因此仅在 `put('manifest_delta', ...)` 前删除字段是不够的。实现必须在任何
live merge **之前**按结构化 `kind == "knowledge_base_document"` 拆分 delta：

```text
extract_manifest_delta_from_tool_event(...)
  -> kb_candidate_delta       # 只进入当前 worker 的 CandidateRegistry
  -> public_live_delta        # 已从 references[] 和 turns[].references[] 删除 KB document
  -> merge public_live_delta into STREAM_LIVE_MANIFEST
  -> emit public_live_delta as manifest_delta
```

不能按工具名前缀、文件名或 `reference_id` 前缀猜测类型。拆分 helper 必须同时处理顶层 `references[]` 与每个
`turns[].references[]`；删除 KB reference 后为空的 turn delta 不应作为空噪声发送。`GET /api/session/manifest`
合并 live cache 前应再次使用同一 public-live projection 作为防御性校验，保证旧 worker 或未来调用点也不能把本轮
KB candidate 从 live cache 带出。若持久化 transcript 在 stream 完成前已经可被 Manifest builder 读取，builder
还必须按当前 `active_stream_id + turn_key + worker_generation` 排除本轮尚未结算的 KB references；历史已结算 turn
的 references 不受影响。

token 不等于 `reference_id` 或 `chunk_id`，且不得由用户文本、MCP 原始结果或模型自行构造。它是
`secrets.token_bytes(12)` 的无填充 Base64URL 编码，必须匹配 `^[A-Za-z0-9_-]{16}$`；版本、类型和完整身份由
CandidateRegistry schema 管理，不重复放入模型输出。建议形式：

```text
K7x8pQm2Vt4zAa9B
```

因此模型实际回显的内部标记固定为 22 个 ASCII 字符：`[[c:` + 16 字符 token + `]]`。这只是当前 run 的短索引；
它不编码版本、文档或权限信息，不能脱离 CandidateRegistry 单独解释。

96-bit 是本方案第一阶段的最小随机量：token 仅在当前 stream 的完整 CandidateScope、短生命周期 registry 与一次
settlement 内有效，但仍须抵抗模型或调用方对其它候选的猜测。不得把它改为 `1`、`a` 或按工具结果顺序递增的编号；
顺序号可被模型在未收到 `_cite` 时凭空构造，破坏“回显当前 host 实际交给模型的 candidate”的审计含义。若未来还需
缩短，必须重新评估最大尝试次数、TTL、并发量和跨通道泄露风险，不能只改正则长度。

服务器在内存中的 candidate entry 至少保存：

```json
{
  "token": "K7x8pQm2Vt4zAa9B",
  "profile": "default",
  "session_id": "session_123",
  "stream_id": "stream_123",
  "worker_generation": 7,
  "turn_key": "turn:42",
  "reference_id": "kbdoc:v1:Fb8...",
  "chunk_id": "kbchunk:v1:Q3c...",
  "source": {"tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments", "tid": "call_01"}
}
```

Candidate entry 的完整身份固定为 `profile + session_id + stream_id + worker_generation + turn_key + tid + token`，并且
entry 内必须保存唯一的 `reference_id + chunk_id`。`profile` 必须是
worker 启动时解析并冻结的规范 Profile identity；默认 Profile 也要使用明确的非空 identity。`worker_generation`
取本次 session owner 的权威 `active_stream_generation`，创建、读取、reserve 和 consume 时都必须重新确认
`session.active_stream_id` 与 generation 仍属于当前 worker。不能只检查 stream ID，也不能在结算时从当前全局
Profile 状态重新推断 identity。

`tid` 是 Citation 安全边界的一部分。Citation candidate 只接受满足
`^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$` 的原始 `tid`；空值、首尾空白、超长、控制字符或其它不合规值均只允许继续
普通工具流程，不得创建 token。不要将多个空 `tid` 合并成同一个调用身份。

这条绑定不可省略：同一 `(kbName, fileName)` 可能由不同 `tid`、不同工具或不同 chunk 重复命中，而既有
Manifest 文档级合并会将 `source[]` 与 `chunks[]` 分别去重，不能反向推导某个 chunk 属于哪个 `tid`。因此一个
token 只代表其产生时的单次真实工具结果；结算出的 Citation `source` 与 evidence snapshot 中的 source 必须等于
该 token 的唯一 source，不得从聚合后的 Manifest `source[]` 猜选另一个调用。

candidate entry 的拥有者是 stream worker。token 的成功状态只能单向流转：
`active -> reserved(settlement_id) -> attached_pending -> committed/consumed`。Agent hook 准备 final assistant content 时
原子 reserve；WebUI 独立复验并把 Citation 暂时附加到内存 message 后才进入 `attached_pending`；只有 `s.save()`
成功后才能标记 `committed/consumed`。同一 token 随后不能再被任何 settlement 使用。

同一 raw final 内一个 token 出现多次时，所有位置复用该 chunk 的同一个 ordinal、Citation 与 evidence；整个
settlement commit 后 token 只消费一次。任一校验失败、`s.save()` 失败、worker error/cancel/替换或 teardown 都进入 `invalidated` 失败终态：撤销
本次内存 message 修改、删除 SettlementRegistry record，并使 reservation 不可复用。持久化最终 Citation 后不保留
token。不能在 `s.save()` 前将 token 标记为 consumed，否则保存失败会形成无法重试的半提交状态。

### 4.2 将 token 安全交给 Agent

现有 `tool_complete_callback` 虽然发生在工具结果写回模型上下文之前，但它的返回值会被忽略；它只能让
WebUI 收集 candidate，不能把 token 交给模型。现有插件 `transform_tool_result` 也不能作为本功能的信任
边界：它会改写原始工具结果，随后知识库 MCP 结果仍会被包在 `<untrusted_tool_result>` 中，且插件本身可由
用户配置。

需要在 Hermes Agent 增加一个 **host-owned**、非插件的 `KnowledgeBaseCitationHook` 接口。WebUI 为每次
`run_conversation()` 创建一个实现对象，并在创建时冻结完整 `CandidateScope`。Agent 只在该次调用的局部变量中持有
它，不把它保存在可跨轮复用的 `AIAgent` 实例上。

```python
@dataclass(frozen=True)
class CandidateScope:
    profile: str
    session_id: str
    stream_id: str
    worker_generation: int
    turn_key: str


class KnowledgeBaseCitationHook(Protocol):
    def annotate_tool_content_for_provider(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        content: Any,
    ) -> Any | None:
        """复制 canonical tool content，并只在 provider 副本的候选 chunk 对象中增加 _cite。"""

    def prepare_final_assistant(
        self,
        *,
        raw_content: str,
    ) -> PreparedAssistantContent:
        """返回无内部 marker 的公开正文，以及可选的 reserved settlement ID。"""
```

```python
@dataclass(frozen=True)
class PreparedAssistantContent:
    content: str
    settlement_id: str | None


@dataclass(frozen=True)
class CitationSettlementPayload:
    scope: CandidateScope
    public_content: str
    citations: tuple[VerifiedCitation, ...]
    evidence: tuple[CitationEvidence, ...]
    reserved_tokens: frozenset[str]


@dataclass
class CitationSettlementEntry:
    settlement_id: str
    payload: CitationSettlementPayload
    state: Literal["reserved", "attached_pending"]
    rollback_snapshot: dict | None = None
```

`CitationSettlementEntry` 只在 prepare 到 commit 的窗口内存在；commit 后从 SettlementRegistry 删除并将候选 token 标记为
`committed/consumed`，失败或取消后删除并将 token 标记为 `invalidated`。因此 `state` 不需要把这些终态继续保留在
内存 entry 中。

`VerifiedCitation` 和 `CitationEvidence` 必须是内部只读 value object；只有持久化或公开投影时才序列化为 `dict`。
不能在 `frozen=True` dataclass 中保存可继续修改的 `list[dict]`，否则 payload 仍可在复验前被篡改。

WebUI 通过本轮 `run_conversation(..., knowledge_base_citation_hook=hook)` 参数注入对象。没有传入时 Agent 保持现有
行为。该对象不是全局单例、AIAgent 构造参数或用户配置项，也不通过 plugin registry 发现。Agent 的内部 `turn_id`
只可用于诊断，不能替代 WebUI 冻结的 `turn_key`，也不能参与重新构造 `CandidateScope`。
这意味着 Hermes Agent 需要新增一个本轮级的 `run_conversation()` keyword 参数；不能把 hook 塞进可跨轮复用的
`AIAgent` 成员后再依赖回调刷新来切换 scope。

`annotate_tool_content_for_provider()` 返回 `None` 表示本次不注解；异常、空返回或超限也按 `None` 处理，provider
继续使用不含 host token 的 canonical content 副本。`tool_call_id` 必须与 CandidateRegistry 中该次调用的 `tid`
一一对应；不能按工具名、最近一次调用或数组位置查找。方法不得原地修改传入的 message 或嵌套 content。

`prepare_final_assistant()` 必须始终返回不含内部 marker 的 `content`。没有合法 Citation 时
`settlement_id=None`；存在合法 Citation 时，hook 在 WebUI-owned SettlementRegistry 创建 `reserved` entry，只把
`content + settlement_id` 交给 Agent。缺失、异常或返回不合规时，Agent 自己执行严格 marker 清理后 fail closed。

`KnowledgeBaseCitationHook.annotate_tool_content_for_provider()` 只在 provider-facing `api_messages` 投影阶段调用；
sequential / concurrent 工具执行仍通过既有 `tool_complete_callback` 同步建立 CandidateRegistry。准确时序是：

```text
工具执行
  -> WebUI tool_complete_callback（收集 candidate token）
  -> tool-result persistence / active-model content projection
  -> make_tool_result_message()（构造不含 _cite 的 canonical tool message）
  -> messages.append(tool_message)
  -> 既有工具进度 state.db flush（只看到原始 canonical tool message）
  -> 下一轮模型调用
      -> 从 messages 构造 provider-facing API 副本
      -> 完成既有 tool-call 配对修复和 provider message 清理
      -> 对仍会实际发送的匹配 tool message 调用 annotate_tool_content_for_provider()
      -> 在复制出的每个 chunk 对象中删除外部同名字段并写入对应 host token
      -> 仅把带 _cite 的副本发送给 provider
```

**不能**把 `_cite` 写入 canonical `tool_message.content`。Agent 在每个工具结果追加后会把 canonical `messages`
增量写入 `state.db`；若直接改 content，candidate token 会进入持久化 transcript，并可能在会话重放或外部 session
payload 中泄露。Hook 必须对 provider 副本操作，不得原地修改 canonical message；无需为 annotated content 新增
ephemeral map 或其它第二份工具结果缓存。

构造下一次 provider 请求时，Agent 已经会从 canonical `messages` 复制出 `api_messages`。在这份副本中，只对同一
`role=tool`、同一 `tool_call_id` 调用 hook。两个工具都返回 document-grouped JSON；hook 在每个
通过严格解析的 `type == "Document"` 对象，遍历其 `chunks[]`，仅在每个合法 chunk 对象自身加入
`"_cite":"<token>"`，使 token 与它对应的 `page_content` 和 chunk ID 天然相邻；Document 对象本身不加 `_cite`。
完整结果仍由既有 `<untrusted_tool_result>` 包裹；canonical message、WebUI Session 和 Agent `state.db`
都不包含服务端生成的 candidate token。

Hook 每次 provider 调用都从当前 CandidateRegistry 重新生成 annotation，因此没有额外 override 生命周期。当前 Agent
run 的正常完成、error、cancel、worker replacement 和 teardown 仍必须清理 CandidateRegistry，禁止 token 跨 run/turn
复用。

模型看到的 provider-only 工具结果形态例如：

```text
<untrusted_tool_result source="mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments">
{"result":"[{\"type\":\"Document\",\"metadata\":{...},\"chunks\":[{\"page_content\":\"片段一\",\"_cite\":\"K7x8pQm2Vt4zAa9B\"},{\"page_content\":\"片段二\",\"_cite\":\"P4t9Bc2mV7qLs5Xe\"}]}]"}
</untrusted_tool_result>
```

`_cite` 是 host 在构造单次 provider 请求时，对**已成功严格解析的 chunk 副本**增加的保留字段。每个带有可引用
`page_content` 的 chunk 都有自己的 token；该 token 在 CandidateRegistry 中绑定唯一的 Document、chunk 和真实工具调用。
模型回显某个 token 时，host 直接得到一个 `reference_id + chunk_id`，不会把同一 Document 下的其它 chunks 一并计入。
Provider 调用结束后这些副本即可丢弃。因此 `_cite` 不属于 MCP 的持久化 wire schema，不要求 MCP
服务新增字段，也不会改变 Agent canonical tool message、重放数据或 WebUI Session。若结果无法严格解析并安全复制，
hook 必须保持 canonical content 不变且不创建 candidate annotation。

模型看到 `_cite` 后仍可能漏标、错标或受检索正文中的提示注入影响；因此“真正使用”的可审计定义是：模型在最终正文
的对应位置回显了本轮 host 实际发给它的 token，并且该 token 通过完整 CandidateScope 与 settlement 校验。服务端
不能据此声称已经证明模型内部推理过程；需要更强的语义证明时，应另加基于答案与 chunk 的一致性评估。

`_cite` 是保留字段。hook 必须在 provider 副本的每个 chunk 对象上先删除 MCP 原始结果携带的同名字段，再写入当前
CandidateRegistry 为该 chunk 生成的 token；Document 或其它层级出现的 `_cite` 只按普通不可信文本处理。模型输出仍不受信任，只有严格匹配
当前 Registry 和 CandidateScope 的 token 才能结算。`reference_id`、`chunk_ids`、`source` 和完整 scope 不进入模型
上下文，只保存在 WebUI 中。

Agent system prompt 同时约束：若最终答案真正依据某 candidate 给出事实性陈述，紧随对应陈述输出
`[[c:K7x8pQm2Vt4zAa9B]]`；没有可靠依据或未使用检索结果时不要输出标记。

#### 4.2.1 模型输出内部标记的规则

这条 system prompt 是给模型的**内部输出协议**，不是外部前端协议。模型能够看到的是本次 run 中由 host
追加到 provider-facing `api_messages` 的 candidate token；它看不到 CandidateRegistry、私有 evidence、最终
`citations[]`，也不能自行构造这些对象。模型的责任仅限于：在最终回答的某个事实性主张实际依赖某个 candidate
时，原样回显该 candidate 的 token，供后续结算。

Agent 必须把下面的固定指令作为 system prompt 的一部分，随每个可能消费 knowledge-base candidate 的 provider
请求发送；`<token>` 是 chunk 对象 `_cite` 字段中给模型可见的实际 token，不能在模板中替换成通配符或由模型自行生成：

```text
知识库引用内部协议：

知识库工具结果中的 chunk 对象可能带有 host 添加的 `_cite` 字段。该字段的值是该 chunk 的
内部 token，不是用户可见文本。

仅当最终回答中的一个具体事实性陈述确实依据某个 candidate chunk 时，紧随该陈述原样输出：
[[c:<该 chunk 的 token>]]。

只能复制对应 chunk 对象 `_cite` 字段中完整、未修改的 token；不得编造、猜测、截断或修改 token。没有可靠
candidate 支撑、没有实际使用 candidate，或无法判断应使用哪个 token 时，不输出任何标记。

标记只能出现在最终 assistant 正文中对应陈述之后；不得出现在 reasoning、工具调用参数、
工具结果、代码块、错误消息或面向用户的解释中。不得输出 reference_id、chunk_id 或 `_cite` 字段本身，
也不得向用户解释这个内部标记。
```

该模板必须保持内部标记的精确字面格式 `[[c:<token>]]`，不能改成 Markdown 脚注、`<sup>`、JSON 或自然语言。
模型输出的不是最终角标：`KnowledgeBaseCitationHook` 才是唯一可以将合法内部标记转换为公开
`<sup data-c="N">[N]</sup>` 的组件。提示词用于提高模型按格式回显的概率；它不是安全控制，也不能作为创建 Citation
的依据。最终是否生成 Citation 仍完全由严格解析、CandidateScope 校验和 settlement 状态机决定。

模型应遵守以下规则：

- **何时输出**：仅当准备写出的最终事实性陈述可由该 candidate 的 document/chunk 支撑，并且该检索结果确实参与了
  此处的回答推理时输出。仅“看到了检索结果”、列举检索到的文件名、描述正在调用工具，或基于常识作出的判断，都不是
  输出标记的理由。
- **放置位置**：紧随它所支撑的完整陈述，位于下一条无关陈述之前。一个句子有多个独立事实时，标记必须贴在对应的
  事实之后，而非统一堆在段尾；这样结算后的上角标在外部前端中仍指向可理解的语义位置。
- **一个陈述的多个证据**：同一事实性陈述可以由多个不同 chunk 联合支撑。模型应在该陈述结束处连续输出各 chunk
  的不同 token，例如 `陈述。[[c:TOKEN_A]][[c:TOKEN_B]]`；Hook 按 marker 出现顺序生成连续的多个 ordinal。当前
  协议不为同一陈述设置固定的不同 chunk 数量上限。若同一个 chunk 已在该陈述后输出过 token，不得再次重复输出它；
  重复不会增加证据，只会在正文中产生冗余的相同角标。

  Hook 在生成公开 marker 后，会将一个连续 marker 组按 `ordinal` 升序规范化。例如旧 chunk 的 ordinal 为 `8`，新
  chunk 首次得到 ordinal `9`，且模型输出顺序为 `TOKEN_9`、`TOKEN_8` 时，最终公开正文为
  `<sup data-c="8">[8]</sup><sup data-c="9">[9]</sup>`。连续组仅允许 marker 之间出现空格或制表符；普通正文与
  换行都会切断分组，避免跨陈述重排。该规则由 settlement 执行，不能依赖模型提示词，因为模型不知道最终 ordinal。
- **使用精确值**：输出格式必须是 `[[c:<token>]]`，其中 `<token>` 必须逐字复制对应 chunk 对象的 `_cite`
  字段值，例如 `[[c:K7x8pQm2Vt4zAa9B]]`。不得编造、截断、改写 token，也不得输出 `reference_id`、
  `chunk_id`、CandidateRegistry 内容或 `_cite` 字段本身。
- **一对多关系**：同一 chunk 若支撑了回答中两个相互独立的陈述，可以在两个位置各输出一次同一 token；Hook
  会将两个位置替换为相同的 `ordinal`，并只生成一个 Citation/evidence。不同陈述分别依赖不同 chunk 时，应各自
  输出对应 token。一个 token 不表示“本轮全部检索结果”，也不能代表同一 Document 下的其它 chunk。
- **禁止出现的位置**：不在 reasoning、工具参数、后续 tool call、工具结果转述、markdown 代码块、错误消息或对用户的
  引用说明中输出。内部 token 只允许作为最终 assistant 正文中的结算占位符。
- **无法可靠归因时**：宁可不输出。缺少可支撑的 candidate、candidate 只包含不相关片段、结论超出片段可证明范围，
  或不确定应对应哪个 token 时，都不产生内部标记。没有标记的文本仍可正常返回，只是不生成知识库引用。

例如，模型在本轮看到某个 chunk 的 `_cite` 为 `K7x8pQm2Vt4zAa9B`，该 chunk 说明“运行部门与科研单位建立
前后台工作机制，并开展仿真校核”。当模型要回答“该通则要求运行部门与科研单位建立前后台协同机制”时，raw final
output 应为：

```text
该通则要求运行部门与科研单位建立前后台协同机制。[[c:K7x8pQm2Vt4zAa9B]]
是否应增加独立预算，现有检索片段没有提供依据。
```

第二句虽然和用户问题相关，但没有被该 chunk 支撑，因此不能附带 token。若回答中还引用了另一个 chunk，则其
内部标记紧随那个 chunk 支撑的陈述，而不是复用 `K7x8pQm2Vt4zAa9B`。

如果同一 Document 的另一个 chunk（例如 token 为 `P4t9Bc2mV7qLs5Xe`）支撑另一条事实，模型必须分别输出两个
token，不能因为它们属于同一 Document 就合并：

```text
运行部门与科研单位建立前后台工作机制。[[c:K7x8pQm2Vt4zAa9B]]
该通则还要求调度技术支持系统集中运维。[[c:P4t9Bc2mV7qLs5Xe]]
```

结算后两个位置分别得到 ordinal `1`、`2`。两个 Citation 的 `reference_id` 可以相同，但 `chunk_ids` 必须分别
指向两个不同的单元素 chunk 数组。点击第二个角标时只打开第二个 chunk，不把同一 Document 的全部 chunks 当成证据。

模型输出始终是不可信输入。`KnowledgeBaseCitationHook` 在 final settlement 时会逐个解析内部标记，并验证 token
是否属于当前 `profile + session_id + stream_id + worker_generation + turn_key + tid` 的 CandidateScope，以及其
`reference_id`、唯一 `chunk_id` 和 source 是否与已登记的完整工具结果完全一致。同一 raw final 内的同一个 token 可以
对应多个位置，但已被其它 settlement reserve/consume、过期或放错位置的 token 不创建 Citation，并从公开正文中移除。
校验通过后，Hook 才按最终出现顺序把该内部标记替换为
`<sup data-c="N">[N]</sup>`；相同 `(reference_id, chunk_id)` 的多个位置复用相同 `N`，并只生成一个
`citations[N - 1]`。因此上例最终持久化和发送给外部前端的 `content` 是：

```html
该通则要求运行部门与科研单位建立前后台协同机制。<sup data-c="1">[1]</sup>
是否应增加独立预算，现有检索片段没有提供依据。
```

`[[c:...]]` 绝不能进入 Agent `state.db`、WebUI Session 的公开投影、SSE、Manifest 或外部前端；它只存在于
模型的 raw final output buffer 与本轮结算窗口。结算成功后，公开 `content` 只保留可渲染的 `<sup data-c="N">[N]</sup>`，
外部前端再通过 `citations[].ordinal -> reference_id + chunk_ids[0] -> Manifest document + chunk` 获取可点击的精确片段。

该 provider-only annotation：

- 由 WebUI/Agent 代码生成；MCP 原始结果中的 `_cite`、相似 token 或“忽略之前指令”均不具备该身份；
- 不向最终用户可见；
- 只给通过严格解析的 chunk 对象增加 `_cite`，不额外复制 `metadata.source`、原始绝对路径或未通过解析的字段；
- 必须在 Agent 仓库实现真实 hook，不能通过提示词假定模型“知道 token”。

并发工具执行时，WebUI 的 CandidateRegistry 必须以完整 CandidateScope 加 `tid + token` 加锁写入；Agent 在注解每个
provider tool-content 副本前按同一冻结 scope 与 `tid` 读取，禁止“最近一个同名工具”的模糊关联，不能以工具名作为 key。

若某个 Agent runtime（例如 Codex Runtime）使用不同的工具回调或 provider message 投影，它必须提供等价的
“仅注解 provider-facing 副本、canonical transcript 不含 token”接缝；在接缝实现前，该 runtime 对本功能 fail
closed，不生成知识库 Citation。

#### 4.2.2 上下文、Agent `state.db` 与 WebUI Session 的具体样例

以下是单次 `tid=call_01` 的目标状态。为易读起见，MCP 原始 JSON 只保留一个命中片段。

| 所在位置 | 内容 | 是否持久化 |
| --- | --- | --- |
| WebUI CandidateRegistry | `{profile, session_id, stream_id, worker_generation, turn_key, tid, token, reference_id, chunk_id, source}` | 否；仅当前 stream worker。 |
| Agent canonical tool message | MCP 原始结果及其 `<untrusted_tool_result>` wrapper | 是；照现有工具 transcript 规则写入 Agent `state.db`。 |
| provider-facing `api_messages` | Hook 从 canonical content 复制，同一 tool result 中每个对应 chunk 对象内含独立 `_cite` | 否；只在单次 provider 请求中存在。 |
| 模型 raw final output buffer | `正文[[c:K7x8pQm2Vt4zAa9B]]` | 否；等待最终结算。 |
| Agent `state.db` 的最终 assistant content | `正文<sup data-c="1">[1]</sup>` | 是；不得含服务端生成的 candidate token 或内部 marker。 |
| Agent 返回给 WebUI 的 final settlement handle | `{settlement_id}`；不含权威 Citation metadata | 否；只供本轮 transcript merge。 |
| WebUI Session `s.messages` | 同一公开 content，外加 `citations[]` 与私有 evidence snapshot | 是；由 `s.save()` 持久化。 |

以下使用 session `09dc270b9271` 中一次“电网高质量发展”检索的真实 MCP 命中完善说明。为避免把完整 502 字
原始 passage 写入仓库，正文仅作摘要；工具名、`tid`、文档身份、分数和按完整 passage 计算出的稳定 ID 均来自
该 session 的 completed tool result。

单库检索调用：

```json
{
  "tid": "call_9e8c32f6c7cc411485b38adc",
  "name": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
  "args": {"kbName": "share49", "query": "电网高质量发展", "topK": 10},
  "result_document": {
    "metadata": {
      "kbName": "share49",
      "fileName": "国家电网公司电网调度控制管理通则_1496838001203.docx"
    },
    "chunk": {
      "summary": "第四章“支撑保障”涉及运行部门与科研单位前后台工作机制、仿真校核和调度技术支持系统集中运维。",
      "score": 0.7391068935394287
    }
  }
}
```

对该 session 当前的 `GET /api/session/manifest` 响应做只读核对可见：这篇文档已被聚合为一个
`kind: "knowledge_base_document"` reference，`source[]` 同时含上述单库调用和下文的 Across 调用；但当前实现
仍返回 `"id": null`，chunk 也没有稳定 ID。因此它还不能供外部前端以 Citation 的 `reference_id` 精确查找。
下面的 `kbdoc:` / `kbchunk:` 值是本提案按该次**完整** MCP 结果计算、并要求上线后写入 Manifest 的目标值，
不是声称当前生产响应已经具备这些字段。

该真实结果没有不可变 `document_id`，捕获数据也没有可验证的租户/账户命名空间，因此按 §2.1 必须降级到
`profile=default + session_id=09dc270b9271` 的 session-scoped document identity，不能再按同名文件生成全局 ID。
以完整的 502 字 `page_content` 和上述 session scope 可计算如下目标 ID。`reference_id` 的 hash 后缀只取决于
`document_key`。该结果没有可信不可变 `chunk_id`，因此 `chunk_id` 必须将单次调用的 `tool + tid`、文档行号、
chunk 行号和完整正文纳入 call-scoped fallback；不能仅按正文生成，也不能与 Across 调用强行复用同一个 chunk ID。
上线前应将下面的 chunk 占位值替换为从完整原始结果计算出的 v1 回归测试向量：

```json
{
  "reference_id": "kbdoc:v1:BpN4DxNf0XW_NYZCDD6uUHrP7l-E8to1XvkEkbhT138",
  "chunk_id": "kbchunk:v1:<single-call base64url(SHA-256(v1_chunk_key))>",
  "source": {
    "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
    "tid": "call_9e8c32f6c7cc411485b38adc"
  }
}
```

Agent canonical tool message（也是此时写入 Agent `state.db` 的 tool content）保留完整 MCP 原始结果及其
`<untrusted_tool_result>` wrapper；下例只缩写正文：

```text
<untrusted_tool_result source="mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments">
{"result":"[share49 / 国家电网公司电网调度控制管理通则… / 完整 502 字 chunk]"}
</untrusted_tool_result>
```

WebUI 仅在当前 stream 内存生成随机 handle，并绑定这个单库调用：

```json
{
  "token": "K7x8pQm2Vt4zAa9B",
  "tid": "call_9e8c32f6c7cc411485b38adc",
  "reference_id": "kbdoc:v1:BpN4DxNf0XW_NYZCDD6uUHrP7l-E8to1XvkEkbhT138",
  "chunk_id": "kbchunk:v1:<base64url(SHA-256(v1_chunk_key))>"
}
```

因此只有发给模型的 API 副本才给对应 chunk 对象增加 `_cite`；为易读省略其它字段：

```text
<untrusted_tool_result source="mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments">
{"result":"[{\"type\":\"Document\",\"metadata\":{...},\"chunks\":[{\"page_content\":\"完整 502 字 chunk\",\"_cite\":\"K7x8pQm2Vt4zAa9B\"}]}]"}
</untrusted_tool_result>
```

同一 session 的 Across 调用使用了多库参数，返回正文与单库结果相同，但由于两次结果都没有可信不可变
`chunk_id`，本方案将它们视为同一 document 下的两个不同 call-scoped chunks，不能声称是同一个 chunk：

```json
{
  "tid": "call_eb2c97bd88ad450d90da3b2e",
  "name": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
  "args": {
    "kbNames": ["share49", "share20", "personal142"],
    "query": "电网高质量发展",
    "topK": 10
  },
  "reference_id": "kbdoc:v1:BpN4DxNf0XW_NYZCDD6uUHrP7l-E8to1XvkEkbhT138",
  "chunk_id": "kbchunk:v1:<across-call base64url(SHA-256(v1_chunk_key))>",
  "source": {
    "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
    "tid": "call_eb2c97bd88ad450d90da3b2e"
  }
}
```

这说明同一 document 可由两个不同工具实际返回，但在缺少可信不可变 chunk ID 时，只有 `reference_id` 可以相同；
两个 call-scoped `chunk_id`、candidate handle 与 source 必须不同。Across 的模型上下文使用同一结构：

```text
<untrusted_tool_result source="mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross">
{"result":"[{\"type\":\"Document\",\"metadata\":{...},\"chunks\":[{\"page_content\":\"同一完整 502 字 chunk\",\"_cite\":\"N5r1Lw9sHx3Qe8Za\"}]}]"}
</untrusted_tool_result>
```

若模型采用单库检索，raw final output 可以是：

```text
该通则提出建立运行部门与科研单位前后台工作机制，以支撑运行技术创新。[[c:K7x8pQm2Vt4zAa9B]]
```

host settlement 验证完整 CandidateScope、`tid + reference_id + chunk_id` 后，Agent `state.db` 的最终 assistant
content 是：

```html
该通则提出建立运行部门与科研单位前后台工作机制，以支撑运行技术创新。<sup data-c="1">[1]</sup>
```

WebUI Session 单独持久化的最终 assistant message 的公开字段如下；同一消息还会保存 §4.4 定义的私有 evidence，
此处不重复展开：

```json
{
  "role": "assistant",
  "id": 42,
  "content": "该通则提出建立运行部门与科研单位前后台工作机制，以支撑运行技术创新。<sup data-c=\"1\">[1]</sup>",
  "citations": [{
    "citation_id": "kbcite:v1:7f2a...",
    "ordinal": 1,
    "reference_id": "kbdoc:v1:BpN4DxNf0XW_NYZCDD6uUHrP7l-E8to1XvkEkbhT138",
    "chunk_ids": ["kbchunk:v1:<base64url(SHA-256(v1_chunk_key))>"],
    "source": {
      "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
      "tid": "call_9e8c32f6c7cc411485b38adc"
    }
  }]
}
```

若模型实际采用 Across token，则公开 marker 和 `reference_id` 可以相同，但 `chunk_ids[0]` 必须是 Across 的
call-scoped chunk ID；Citation `source` 和私有 evidence snapshot 都必须记录 Across 的真实 `tool + tid`，不能因为
document 正文相同而误记为单库检索。外部前端用该 message 的 `citations[0].reference_id` 定位 Manifest document，
再用 `citations[0].chunk_ids[0]` 定位该 document 中的唯一实际片段。

模型的 `[[c:...]]` 同样不能先落入 Agent `state.db`。因此同一个
`KnowledgeBaseCitationHook.prepare_final_assistant()` 在最终无 tool-call assistant message 构造完成、但
`messages.append(final_msg)` 和任何 state.db flush **之前**运行。WebUI 注入的 hook 使用冻结 scope 和当前 stream 的
CandidateRegistry 验证内部 marker，原子 reserve token，并把权威 `CitationSettlementPayload` 留在 WebUI-owned
SettlementRegistry。Agent 只取得 `settlement_id + content`：用公开 content 写 canonical final message，并通过本轮非持久化
结果把 settlement ID 交回 WebUI；Agent 现有 state.db message schema 只存 content，不存候选 token、Citation metadata
或 settlement record。

`KnowledgeBaseCitationHook` 缺失、失败或返回不合规时必须移除全部 `[[c:...]]`，返回普通公开文本且不含 `citations`。
这保证失败不会让内部 token 进入 Agent `state.db`、WebUI Session 或客户端。

### 4.3 最终回答结算

最终结算有两个持久化边界，且使用同一份 WebUI-owned authoritative settlement record：

1. **Agent 边界：** `KnowledgeBaseCitationHook.prepare_final_assistant()` 在最终 assistant message 写入 Agent `state.db` 前，
   将 raw internal token 转为公开 content，reserve token，并在 host registry 留下 payload；Agent 只持久化公开 content。
2. **WebUI 边界：** transcript merge 后，不信任 Agent 返回的 Citation 字段，只按 `settlement_id` 从自己的 registry
   取回 record，针对当前 Session/worker/message 独立复验；复验成功后将 canonical metadata 暂时写入本轮最终
   `s.messages` assistant message，并把 reservation 置为 `attached_pending`。只有随后 `s.save()` 成功，才能原子
   commit/consume token。

该 settlement 的验证规则如下：

1. Agent hook 已从**仅存在于运行内存的 raw final buffer**提取严格格式的
   `[[c:<token>]]`，并按出现顺序为每个有效 token 分配 `ordinal`；WebUI 收到的 final assistant
   `content` 必须已经是没有内部标记的 `public_content`。严格语法为 `\[\[c:([A-Za-z0-9_-]{16})\]\]`：无空白、
   无引号、无属性、无额外字符。只有捕获出的完整 token 才进入 CandidateRegistry 校验；格式畸形、未登记、过期或
   不属于当前 scope 的 `[[c:...]]` 均从公开 content 删除，且不占 ordinal。
2. token 必须同时满足：属于当前
   `profile + session_id + stream_id + worker_generation + turn_key + tid`、尚未被其它 settlement reserve/consume、
   关联工具在本轮成功 completed、工具名在白名单内、`tid` 合规且与 token 的单一 source 一致，并且 token 登记的
   唯一 `reference_id + chunk_id` 仍存在于该工具调用的完整 candidate 结果中。
3. hook 验证通过后，将内部 token 替换为公开 marker `<sup data-c="N">[N]</sup>`，并将候选的 canonical
   `citations[]` 留在 host-owned record；WebUI attach 只能复制该 record，不得再次从 public content 解析 token。
4. 无效、重复但没有不同使用位置、跨 turn、未知、失败、取消、缺失候选或格式错误的 token 必须从可见
   `content` 移除，且不生成 Citation。
5. Agent 可以先持久化不含内部 token 的公开 content；WebUI 随后持久化携带同一公开 content 与 citations 的
   message。只有 WebUI save 成功并 commit settlement 后才允许清理 worker owner 并发出 `done`。
   `done.session.messages` 和随后 `GET /api/session` 必须返回同一 content 与 citations。

#### 4.3.1 `/api/chat/stream` 的具体落点和写入方式

不要在构造 `done` payload 时临时给 JSON 增加 `citations`。那样浏览器本次能看到角标，但会话文件、重连后的
`GET /api/session`、以及 Manifest 重建仍是没有引用的旧 message。唯一的事实来源应是 `s.messages` 中本轮最终的
assistant message。

`api/streaming.py` 的 completed 路径已经在 transcript merge 后取得了最终 assistant message，随后依次执行：

```text
确认未 cancel
  -> Agent 已在 state.db 写入前调用 host hook，得到公开 content + settlement_id
  -> WebUI 内存 settlement: reserved
  -> 完成 transcript merge，得到本轮 final assistant message
  -> 获取 session 写锁，复验 owner/generation + session_revision
  -> attach_verified_knowledge_base_citations(...)  # 新增：复验并进入 attached_pending，保留回滚快照
  -> 清理 active_stream_id / active_stream_generation
  -> s.save()                                   # 唯一 WebUI Session commit point
  -> commit_knowledge_base_citation_settlement(...) # 仅此时 committed/consumed
  -> Session `session.json` 原子保存成功，settlement commit/consume
  -> _persist_turn_artifact_paths(...)
  -> 清理 Citation registries，释放 session 写锁
  -> 组装 done.session（从已保存的 session 读取）
  -> SSE done
```

也就是说，attach 必须发生在第一次 `s.save()` **之前**，commit 必须发生在该 save **之后**；上述步骤必须在同一个
`_get_session_agent_lock(session_id)` 保护下完成，不能让 edit/retry/truncate/另一个 stream 插入中间。
不要在 `on_token`、`manifest_delta` 或 `put('done', ...)` 中重新结算。Gateway 的完成路径要在它自己的“merge 完
最终 message、第一次 save 之前”采用同一个 attach helper，不能只修 `/api/chat/stream` 的一条路径。

当前 `api/streaming.py` 与 `api/gateway_chat.py` 的部分成功路径会在保存 Session 前清空 owner。实现时必须调整为
以下固定顺序，不能照旧位置直接插入 helper：

```text
复验 owner/generation
  -> attach Citation（attached_pending）
  -> 在同一内存事务中清理 owner/generation
  -> 原子保存 Session message + owner 状态
  -> 标记 settlement committed/consumed
  -> done
```

若 `s.save()` 抛错，必须用 attach 返回的回滚快照恢复内存 message，令 settlement/token 失效，清理 registries，并走已有
错误终态；不得清理 owner 后继续报告成功，也不得发送带角标的 `done`。Agent `state.db` 若已写入只有公开 `<sup>` 的
content，该条记录不构成已验证 Citation：后续 merge/公开投影必须删除没有同一条 message `citations[]` 与 evidence
对应的孤立 marker。由于 `session.json` 使用原子保存，`content + citations[] + 私有 evidence` 同时落盘即视为提交，
不再新增 settlement journal 文件。

`CitationSettlement` 和 token reservation 只存在当前 stream worker 的内存中：`prepare` 负责校验并 reserve，
`attach` 在 Session 写锁内把 host-owned 的 `citations[]` 与 evidence 写入目标 message，`s.save()` 成功后
立即 consume 并释放 settlement。进程结束后不会尝试从磁盘恢复 token；读取侧只依据 message 自身的 content、
`citations[]` 和私有 evidence 是否一一对应来决定是否公开角标。

调用方需在 stream worker 闭包中持有下面这些状态：

```python
# 仅示意：实际逻辑放到 integration/knowledge_base/，streaming.py 只保留薄调用。
_kb_citation_candidates: CandidateRegistry  # key: 完整 CandidateScope + tid + token
_kb_citation_settlements: SettlementRegistry # key: settlement_id；只存 host-owned record
_manifest_turn_key: str                      # 本轮稳定 turn key，已有 manifest 逻辑使用
_worker_generation: int                      # worker 启动时冻结的 active_stream_generation
_latest_assistant_idx: int | None            # transcript merge 得到的本轮最终 assistant 下标
```

推荐的跨仓库接缝代码形状如下（不是可直接粘贴的完整实现）：

```python
# Agent 调用 KnowledgeBaseCitationHook.prepare_final_assistant()：在 Agent state.db 写入 final assistant 前运行。
prepared = knowledge_base_citation_hook.prepare_final_assistant(
    raw_content=raw_final_assistant_content,
)
# hook 使用创建时冻结的 CandidateScope，将 payload 留在 WebUI registry；Agent 仅取得 {settlement_id, content}。

# api/streaming.py：completed 分支，cancel 检查之后、第一次 s.save() 之前。
attach_verified_knowledge_base_citations(
    messages=s.messages,
    final_assistant_index=_latest_assistant_idx,
    settlement_id=agent_result.citation_settlement_id,
    expected_scope=current_candidate_scope,
    settlements=_kb_citation_settlements,
)
try:
    s.save()
except Exception:
    rollback_pending_knowledge_base_citations(...)
    raise
else:
    commit_knowledge_base_citation_settlement(...)
```

`final_assistant_index` 应来自本次 transcript merge 的结果，或由相同的 turn 归属规则解析；**不能简单使用
`s.messages[-1]`**。最后一项在某些 provider/工具调用形态下可能是 tool message、partial message 或历史合并项。
若找不到唯一的本轮真实 `role == "assistant"` message，helper 必须 fail closed：不创建 `citations`，并清理该轮
raw content 中可识别的内部标记后返回诊断结果。

WebUI-owned prepare helper 的核心职责是纯 content transformation；下面的 `scope` 与 `candidates` 是 hook 创建时
冻结并持有的内部状态，代码仅展示 helper，不是 Agent 侧再次传入的参数。Hook 将结果连同权威 Citation metadata 保存
为 host-owned payload：

```python
def prepare_knowledge_base_citation_content(
    *,
    raw_content: str,
    scope: CandidateScope,
    candidates: CandidateRegistry,
) -> CitationSettlementPayload:
    marks = parse_internal_citation_marks(raw_content)  # 只接受完整、严格的 [[c:<token>]]
    valid_marks = validate_marks(marks, scope, candidates)

    content, citations = replace_valid_marks_in_order(raw_content, valid_marks)
    return CitationSettlementPayload(
        scope=scope,
        public_content=remove_all_internal_citation_marks(content),
        citations=citations,
        evidence=build_evidence_snapshot(citations, candidates),
        reserved_tokens=frozenset(mark.token for mark in valid_marks),
    )
```

hook 在保存 record 时生成至少 128-bit 随机 `settlement_id`，并在同一锁内把 record 的全部 token 从 `active` 改为
`reserved(settlement_id)`；任一 token 已被 reserve/consume 时整份 prepare 失败，不允许部分 reservation。

`attach_verified_knowledge_base_citations()` 不接受 Agent 返回的 citations/evidence。它只接受 opaque
`settlement_id` 和 host 当前计算的 `expected_scope`，从当前 worker 的 SettlementRegistry 读取 host-owned record，并至少重新检查：

1. record 的 `payload.scope` 与 host 当前计算的 `expected_scope`（`profile + session_id + stream_id + worker_generation + turn_key`）
   完全一致，session 当前 owner 仍是该 stream/generation；settlement 未被消费且所有 token 只被该 settlement reserve。
   记录 attach 前的 `session_revision`。
2. 目标是本轮唯一 final assistant message，message 的公开 content 与 record `public_content` 完全相同；若用 digest
   比较，必须对规范化前的原始 bytes 使用 record 中同一算法，不能对自由文本做宽松归一化。目标 message identity、
   `session_revision` 和 content digest 必须在同一 Session 写锁内再次确认；任何 edit/retry/truncate/并发 stream
   造成 revision 变化都直接 fail closed。
3. `reference_id` 来自该完整 scope 下的 CandidateRegistry；每个 candidate entry 只含一个非空 `chunk_id`，且它属于
   对应 `reference_id`。生成公开 Citation 时必须写成单元素 `chunk_ids: [candidate.chunk_id]`，不得附加同一 Document
   的其它 chunk，也不得通过 page content 相似度、数组位置或检索分数猜测替换 chunk。
4. `source.tool + source.tid` 与 candidate 的唯一成功 completed 调用完全一致，tool 在两个精确白名单内，`tid` 通过
   §4.1 格式校验；不能从聚合 Manifest 选择 source。
5. `citation_id` 在 message 内唯一；`ordinal` 是唯一、从 1 连续递增的正整数；content 中 server-generated marker
   的个数和 `data-c` 集合与这些 ordinal 精确相等，不允许额外 marker、Citation 或 evidence 行。

全部通过后，helper 在同一锁内将 reservation 标记为待提交，保留目标 message 的回滚快照，并只把
**host record** 的 canonical content、citations 和 evidence 写入 message；不得复制 Agent 返回的同名字段。
`s.save()` 成功后，commit helper 在同一锁内把 reservation 原子改为 `committed/consumed`，递增
`session_revision`。找不到唯一
message、record 缺失、scope/content
不一致、重复消费或任一结构校验失败时必须 fail closed：使 settlement/token 失效，清理可识别的内部 marker 和
未经确认的 `<sup data-c>`，不写 Citation/evidence，并记录不含原始 chunk 的诊断。它不得依据自由文本、文件名或
Manifest 聚合结果猜测引用。

`replace_valid_marks_in_order()` 只处理 `validate_marks()` 已返回的条目：按它们在 raw content 中的出现位置，以
`<sup data-c="1">[1]</sup>`、`<sup data-c="2">[2]</sup>` … 替换，并同步生成同 ordinal 的
`citation_id`、`reference_id`、`chunk_ids` 和唯一 `{tool, tid}` source，并写入私有 evidence snapshot。它不能使用 `str.replace(token)` 这类全局替换，以免
相同 token 在不同位置、伪造 token 或原文中的普通文本被误改；应依据 parser 给出的字符区间逐段重建 content。
注入 marker 前还必须中和原始模型文本中的 `<sup>`，使最终保留下来的 `data-c` marker 全部由本 helper 生成。

这是一个 session 内存对象的原地修改，状态层是 **Session transcript**；`s.save()` 是唯一持久化边界。attach 到 commit
必须持有同一个 Session 写锁，并以 `session_revision + message identity + content digest` 做 check-and-use。只有 save
成功后，才能 consume token，并让 `done` 的 `session` 序列化包含新的 `message.citations`。若 save 失败，必须
恢复 attach 前的内存 message、作废 reservation，且不得发送带角标的 done，而应走已有错误终态，防止“客户端
看到引用、刷新后引用消失”。

#### 4.3.2 内部 token 的统一出站隔离

内部 token 不能只在 `/api/chat/stream` 的 `token` 事件中过滤。模型看到 token 后，可能把它写入 reasoning、
`interim_assistant`、后续工具参数，或让它出现在 tool result/preview、`apperror`、partial/cancel snapshot、日志与 turn
journal 中。任何一个通道先泄露 token，外部调用方都可能在同一活动 turn 通过 steer 要求模型回显并使用它；即使
scope 校验阻止跨 turn 复用，也会削弱“最终 marker 来自模型对受信任 candidate 的原始选择”这一审计语义。

因此实现必须提供一个 integration-owned 的统一边界过滤器，并以当前 CandidateRegistry 中**实际生成的完整 token
集合**作为匹配表，而不是只用宽泛正则删除任意 MCP 原文。所有离开受信任模型运行边界的副本都要
经过它：

- 所有 SSE event，包括 `token`、`reasoning`、`interim_assistant`、tool start/complete/preview、`apperror`、partial、
  cancel、done 前的诊断事件以及 Gateway 对应事件；
- WebUI/Agent 的 partial snapshot、cancel snapshot、恢复 journal、turn journal 和结构化错误上下文；
- 日志字段、异常字符串、指标 label 和调试 dump；
- 后续工具调用参数。若参数中含当前 candidate token，不应静默改写后继续执行，而应拒绝该次工具调用或将其标为
  不可执行，避免 token 被发送到 MCP/其它外部工具；对外错误副本仍须经过过滤；
- 从上述内容派生出的 tool result、snippet、preview 或转发 payload。

过滤器必须区分两类数据：受限的 **raw assistant/reasoning buffer** 仅供最终 settlement 解析，不发送、不落公开
持久化；**public buffer/projection** 才能进入 SSE、Session partial、日志和诊断。对于流式文本，过滤器要识别跨多个
frame 拆分的 `[[c:...]]`，完整吞掉内部标记；error/cancel/teardown 时丢弃尚未闭合的标记尾巴。对于
结构化 payload，要递归处理所有字符串叶子，并维持字段白名单，不能先序列化整对象再漏过旁路字段。

最终 `done` 不走 raw buffer：它只能序列化 `s.save()` 后的公开 message。在 `done` 前外部前端暂时看不到角标；
结算后的持久化 content 才携带 server-generated `<sup>` marker。不得通过“日志仅内部可见”或“reasoning 通常不展示”
豁免任何通道，因为这些通道可能被部署侧采集或转发。

这里的“真正使用”是可审计定义：模型在最终答案中使用了当前服务端发给它的可信候选 token，并且该 token
通过服务端的 turn/tool/chunk 校验。它不是无法证明的语义相似度判断。

### 4.4 Manifest 对应关系和持久性

当前 Manifest reference 是 transcript/tool-call 派生索引，并不写入 Artifact store。最终 Citation 一旦进入
持久化 assistant message，就不能依赖短生命周期的 stream token 才能重新关联。

结算时在 assistant message 内保存最小的不可变 evidence snapshot，建议私有字段：

```json
{
  "_knowledge_base_citation_evidence": [
    {
      "reference_id": "kbdoc:v1:Fb8...",
      "kb_name": "share49",
      "file_name": "电力市场运行基本规则.docx",
      "chunk_id": "kbchunk:v1:Q3c...",
      "page_content": "第一章 总则……",
      "score": 0.82
    }
  ]
}
```

该字段是**服务端私有持久化证据**，不是 Citation wire 的一部分。它允许 Manifest 在完整 tool result 已因压缩、
分页或历史裁剪而不可见时恢复被真实引用的文档与 chunk；外部前端的角标映射只使用 §3.3 的公开链路：
`content.data-c -> citations[].ordinal -> reference_id + chunk_ids[0] -> Manifest document + chunk`。

evidence snapshot 也必须遵守 chunk 级最小化：只保存 `citations[].chunk_ids` 实际引用到的 chunks。即使原工具结果中
同一 Document 还有其它 chunks，也不得顺带复制进 snapshot；多个 Citation 引用同一 chunk 时可按 chunk ID 去重保存。

#### 4.4.1 私有持久化与公开投影必须分离

WebUI Session 的磁盘原始 message 可以保存 `_knowledge_base_citation_evidence`，但下列所有出站面都必须先删除该
字段，再序列化或记录：

- `GET /api/session` 返回的 `session.messages`，包括分页、`turn_align` 和历史重放形态；
- `/api/chat/stream` 的 `done.session.messages`；
- Gateway chat 的 `done.session.messages`；
- 所有返回完整 `session + messages` 的 Session mutation 响应，包括 `/api/session/duplicate`、
  `/api/session/truncate`、Session workspace/settings 更新、compression/recovery 和 share create/revoke；
- session JSON export，以及 Markdown/HTML transcript export；
- 公开 share snapshot；share 可继续使用自身的 message 字段白名单，不要求因此新增 Citation 详情能力；
- 任何携带 message/session payload 的日志、turn journal、诊断快照或错误上下文。

`GET /api/session`、两个 chat backend 的 `done` 和结构化 JSON export 必须保留 `content` 与 `citations[]`；
Markdown/HTML export 和 public share 可按既有窄化契约只保留允许字段，但无论是否保留 Citation，都不得包含
evidence。下面分别展示 WebUI 内部持久化消息和对外公开消息；它们不是同一个接口返回对象：

任何 handler 都不得直接拼接 `s.messages`、`session.messages` 或其深拷贝作为响应。必须先调用统一的
`public_session_projection()`；该规则同样适用于“写操作成功后顺便返回 Session”的响应，不因请求本身不是
读取接口而放宽。`duplicate`/`branch`/`compression` 复制历史消息时，先在服务端按 §5.5 清理或重签
Citation，再保存目标 Session，最后对响应再次执行公开投影。

WebUI 内部持久化的原始 message：

```json
{
  "role": "assistant",
  "id": 42,
  "content": "结论。<sup data-c=\"1\">[1]</sup>",
  "citations": [{
    "citation_id": "kbcite:v1:7f2a...",
    "ordinal": 1,
    "reference_id": "kbdoc:v1:Fb8...",
    "chunk_ids": ["kbchunk:v1:Q3c..."],
    "source": {"tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments", "tid": "call_01"}
  }],
  "_knowledge_base_citation_evidence": [{
    "reference_id": "kbdoc:v1:Fb8...",
    "kb_name": "share49",
    "file_name": "电力市场运行基本规则.docx",
    "chunk_id": "kbchunk:v1:Q3c...",
    "page_content": "第一章 总则……",
    "score": 0.82
  }]
}
```

该结构只供 WebUI 自己读取和构建 Manifest，尤其是 `_knowledge_base_citation_evidence` 不属于公开协议。

对外返回的公开 message：

```json
{
  "role": "assistant",
  "id": 42,
  "content": "结论。<sup data-c=\"1\">[1]</sup>",
  "citations": [{
    "citation_id": "kbcite:v1:7f2a...",
    "ordinal": 1,
    "reference_id": "kbdoc:v1:Fb8...",
    "chunk_ids": ["kbchunk:v1:Q3c..."],
    "source": {"tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments", "tid": "call_01"}
  }]
}
```

公开结构由内部消息经过 `public_session_projection()` 得到：保留 `content` 和 `citations[]`，删除
`_knowledge_base_citation_evidence`，也不携带 CandidateRegistry 的短 token 或 SettlementRegistry 的 `settlement_id`。

#### 4.4.2 WebUI 侧存储分层

WebUI 侧不要把所有引用信息都理解成同一种“存储”。本方案有四个边界，只有第一个会随 Session 一起持久化：

| WebUI 状态 | 典型内容 | 存储位置与生命周期 | 是否对外返回 |
| --- | --- | --- | --- |
| **Session 持久化消息** | `role`、`id`、公开 `content`、`citations[]`、私有 `_knowledge_base_citation_evidence` | `s.save()` 管理的 WebUI Session 持久化层；随会话保留，删除消息/会话时一并删除 | 只返回经过 public projection 的版本；不返回 evidence |
| **CandidateRegistry** | `profile`、`session_id`、`stream_id`、`worker_generation`、`turn_key`、`tid`、短 token、`reference_id`、`chunk_id`、真实 source、状态 | 当前 stream worker 内存；从工具 completed 创建，到 settlement 成功或失败终态后释放 | 否；不能作为 Manifest 或 SSE 数据源直接暴露 |
| **SettlementRegistry** | `settlement_id`、raw content digest、规范化公开 `content`、待附加 Citation、reservation 状态、回滚快照 | 当前 stream worker 内存；`prepare -> attach -> s.save() -> commit` 期间存在，完成或失败后删除 | 否；Agent 和外部前端都只能间接得到最终结果 |
| **Manifest 派生结果** | `knowledge_base_document`、稳定 `reference_id`、`chunks[]`、`source[]` | `build_session_manifest()` 只从已验证的最终 message Citation/evidence 按需重建；completed tool result 只产生候选，不直接公开 | 是，但只返回公开 Manifest schema |

WebUI Session 的持久化消息应类似下面的**内部原始结构**。这里的“原始”是指尚未经过对外投影，不代表可以直接
发送给客户端：

```json
{
  "role": "assistant",
  "id": 42,
  "content": "结论。<sup data-c=\"1\">[1]</sup>",
  "citations": [
    {
      "citation_id": "kbcite:v1:7f2a...",
      "ordinal": 1,
      "reference_id": "kbdoc:v1:Fb8...",
      "chunk_ids": ["kbchunk:v1:Q3c..."],
      "source": {
        "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
        "tid": "call_01"
      }
    }
  ],
  "_knowledge_base_citation_evidence": [
    {
      "reference_id": "kbdoc:v1:Fb8...",
      "kb_name": "share49",
      "file_name": "电力市场运行基本规则.docx",
      "chunk_id": "kbchunk:v1:Q3c...",
      "page_content": "第一章 总则……",
      "score": 0.82
    }
  ]
}
```

同一条消息经过 `public_session_projection()` 后，才形成可返回给外部前端的结构：保留 `content` 和 `citations[]`，
递归删除 `_knowledge_base_citation_evidence`，并且不携带 CandidateRegistry 的短 token 或 SettlementRegistry 的
`settlement_id`：

```json
{
  "role": "assistant",
  "id": 42,
  "content": "结论。<sup data-c=\"1\">[1]</sup>",
  "citations": [
    {
      "citation_id": "kbcite:v1:7f2a...",
      "ordinal": 1,
      "reference_id": "kbdoc:v1:Fb8...",
      "chunk_ids": ["kbchunk:v1:Q3c..."],
      "source": {
        "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
        "tid": "call_01"
      }
    }
  ]
}
```

因此，WebUI 侧的引用数据流是：

```text
CandidateRegistry（临时 token）
    -> SettlementRegistry（待提交 Citation）
    -> s.save()
    -> Session 持久化消息（content + citations + 私有 evidence）
    -> public_session_projection()
    -> /api/chat/stream done、GET /api/session、JSON export
```

Manifest 不读取短 token，也不把 CandidateRegistry 当作持久化来源。它只使用 Session 中已验证的 Citation/evidence
和可重建的 completed tool result，最终通过稳定的 `reference_id`、`chunk_id` 对外提供详情。

不能依赖现有 `redact_session_data()` 隐藏 evidence：该函数负责字符串凭据脱敏，会保留 message 中未知字段；并且
`api_redact_enabled=false` 时字符串脱敏可以关闭，而私有字段剥离任何时候都不能关闭。实现时应建立一个共享的
公开投影入口，所有上述出站路径都复用它，不能在每个 endpoint 分别手写删除逻辑：

```python
def public_session_projection(raw_session: dict) -> dict:
    projected = strip_private_knowledge_base_citation_evidence(raw_session)  # 深拷贝并递归删除私有字段
    return redact_session_data(projected)  # 在私有字段已删除后，再执行可配置的文本脱敏
```

`strip_private_knowledge_base_citation_evidence()` 至少要遍历所有嵌套 `messages` 容器，以覆盖分页、重放、export/share
包装和未来新增的 session envelope。它不能原地修改磁盘读取对象，否则后续 Manifest 构建可能失去恢复证据。
已有 `api/shares.py` 的 message 字段白名单仍应保留，统一 strip helper 是附加的防回归边界，不用于放宽 share schema。
Fork 专有的字段识别与递归剥离实现放在 `integration/knowledge_base/citations.py`；`api/helpers.py` 只做 import 与
上述公开投影组合，保持核心接缝足够薄。

`build_session_manifest()` 必须读取未经过公开投影的原始持久化 Session：先从成功 completed 工具结果重建
references，再仅为已验证 Citation 合并 evidence snapshot。合并只能补足同一 `reference_id` 的已引用
chunk/source，不能凭快照新增任意未引用的候选，也不能覆盖真实工具来源。Manifest 的公开响应可以包含由
evidence 恢复出的标准 `knowledge_base_document` reference，但绝不能包含 evidence 字段本身。

这样在上下文压缩、会话分页或历史 transcript 未保留完整 tool result 时，仍能让：

```text
message.citations[].reference_id == manifest.references[].id
```

保持成立。若 snapshot 本身结构不完整、ID 不可重算或 source 不在两个白名单工具内，Manifest 必须跳过它；
不能用数据不完整的证据生成可浏览的假 reference。

## 5. 外部前端消费时序

### 5.1 启动和流式阶段

```text
POST /api/chat/start
  -> { session_id, stream_id }

GET /api/chat/stream?stream_id=...
  -> token                 只渲染尚未结算的回答正文
  -> manifest_delta        可继续处理其它 Manifest 类型；忽略/不发送 knowledge_base_document
  -> done                  取得最终 session.messages 的 content + citations
  -> GET /api/session/manifest
                            以 citation.reference_id 获取引用详情
```

在收到 `done` 前，外部前端不会从 SSE、live cache 或并发的 Manifest GET 看到**本轮新产生**的知识库 document
candidate；历史已结算 turn 的 references 仍可正常读取。`done` 是本轮最终答案与 Citation 的唯一结算边界。
客户端不得为了抢先展示详情而从工具事件、文件名或缓存猜测引用；必须在 `done` 后按
`citation.reference_id` 查询完整 Manifest。

### 5.2 `done` 示例

```text
event: done
data: {
  "session": {
    "session_id": "session_123",
    "messages": [
      {"role": "user", "_turn_key": "turn:42", "content": "..."},
      {
        "role": "assistant",
        "id": 42,
        "content": "根据现有资料，额定电流没有被直接给出。<sup data-c=\"1\">[1]</sup>",
        "citations": [
          {
            "citation_id": "kbcite:v1:7f2a...",
            "ordinal": 1,
            "reference_id": "kbdoc:v1:Fb8...",
            "chunk_ids": ["kbchunk:v1:Q3c..."],
            "source": {
              "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocuments",
              "tid": "call_01"
            }
          }
        ]
      }
    ]
  },
  "usage": {}
}
```

外部前端应以 `done.session.messages` 覆盖本次 stream 的临时 token 文本，而不是把 server-generated `<sup>`
marker 再 append 到已流式渲染的内容上。

### 5.3 获取引用详情

收到 `done` 后，客户端请求：

```http
GET /api/session/manifest?session_id=session_123
```

用 `citation.reference_id` 精确定位 document，读取：

- `metadata.kbName`
- `metadata.fileName`
- `metadata.chunks[]` 中 `id == citation.chunk_ids[0]` 的唯一片段
- `citation.source` 对应的真实 MCP 调用来源

即使同一文档被两个 MCP 工具命中，角标仍只需要一个 `reference_id`；详情页可展示该 Citation token 绑定的
`source`，而不是把文档的所有历史检索来源误称为本次引用来源。

### 5.4 刷新、重连和历史会话

| 场景 | 外部前端行为 |
| --- | --- |
| 断线后收到重放的 `done` | 按既有 message identity 幂等覆盖整条 message；Citation 直接复用持久化 `citation_id`，不重新生成 ID 或角标。 |
| 页面刷新 | 从 `GET /api/session` 读取 `messages[].citations`，再获取 Manifest；不要等待旧 stream 重放。 |
| 未收到或丢失 `manifest_delta` | 不影响知识库引用；引用详情一律以 `GET /api/session/manifest` 的完整响应为准。 |
| 历史 message 没有 `citations` | 当作没有已验证引用；禁止从旧 `references[]` 回填角标。 |
| Citation 的 Manifest reference 缺失 | 保留 content marker，但将详情视为不可用；重试一次完整 Manifest 查询后仍缺失则记录协议错误。 |

### 5.5 会话复制、分支、编辑与压缩

Citation 的公开字段可以随历史 message 复制，但私有 evidence 原本绑定旧的 `profile + session_id`。如果原样复制，
严格按新 session 校验会让历史引用失效；若放宽校验，又会把 evidence 变成可跨会话移植的伪造材料。因此不能把
`duplicate` / `branch` 当前的 message 深拷贝行为直接视为 Citation 继承协议。

首期实现采用以下 fail-closed 规则：

- `duplicate` / `branch`：只有服务端能够从原始可信 Session 读取并验证旧 Citation，且为目标
  `profile + session_id` 重新签发 evidence 时，才保留 `content` marker 与 `citations[]`；不复制 candidate token、
  settlement ID 或旧 scope。暂未实现重签时，原子删除 marker、`citations[]` 与 evidence，不能只删其中一项。
- retry：旧 assistant message 按现有 retry 语义被替换/截断；新回答必须重新执行工具候选与 settlement，不继承旧
  Citation。
- edit / truncate：只要 assistant `content` 发生改变，就原子删除该 message 的全部 server marker、`citations[]`
  与 evidence；不得尝试按 ordinal 或字符串位置猜测哪些旧引用仍有效。
- compression：压缩摘要不能继承被压缩正文的 Citation。原始未改写 message 若仍由服务端保留，其 evidence 继续
  归原 message 所有；若原 message 被替换，则新摘要不带引用，除非它在新的受信任 turn 中重新结算。

普通外部 JSON import 的信任策略不在本提案本期范围内，后续如需支持跨实例保留 Citation，应另行定义签名导出包
或独立可信 Citation store，不能顺带复用上述服务器内部重签路径。

## 6. 状态层、生命周期与失败关闭

### 6.1 状态所有权

| 状态 | 所有者 | 生命周期 |
| --- | --- | --- |
| MCP 原始结果 | Hermes Agent transcript / 已结算 tool call | 由现有会话与 tool-call 契约管理。 |
| CandidateRegistry | 当前 stream worker；按完整 CandidateScope 隔离 | 完整权威 tool result 成功 completed 后创建；token 经 `active -> reserved -> attached_pending -> committed/consumed` 单向流转，失败进入 `invalidated`；所有 terminal exit 释放。 |
| SettlementRegistry | 当前 stream worker；WebUI-owned | Agent hook prepare 时创建 authoritative record；仅 WebUI save 成功并 commit 后，或任一失败/terminal exit 后删除。 |
| `STREAM_LIVE_MANIFEST` | WebUI/Gateway stream 层 | 只保存 public live delta；任何阶段均不得包含本轮 `knowledge_base_document` candidate。 |
| 最终 `content`、`citations`、evidence | 持久化 assistant message | `session.json` 原子写入同一条 assistant message；三者自洽时视为已提交，公开投影仅保留可校验的 `citations[]`。 |
| citation evidence snapshot | 持久化 assistant message（服务端私有） | 与 message 一起保存和删除；只供 Manifest 构建、恢复与审计读取，所有出站投影剥离。 |
| Manifest reference | `build_session_manifest()` 派生投影 | 每次读取从可信 tool evidence 和必要的已引用 snapshot 构建；不写 Artifact store。 |

### 6.2 生命周期矩阵

| 终态 | 是否写 `citations` | 是否修改 final content | candidate token map |
| --- | --- | --- | --- |
| 正常 completed | 是，前提是 Agent prepare、WebUI 独立复验和 `s.save()` 均成功 | 有效内部 token 替换为 marker；无效 token 移除 | attach 后 `attached_pending`，save 成功才 commit/consume，再释放 registries |
| `s.save()` 失败 | 否 | 回滚内存 message；孤立 Agent marker 不得在重放公开 | reservation invalidated，释放 registries，不发送 done |
| provider/error | 否 | 不持久化部分回答中的内部 token；public SSE 已过滤 | 释放 |
| cancel | 否 | 不持久化部分回答中的内部 token；public SSE 已过滤 | 释放 |
| stale/replaced worker | 否 | generation/owner 复验失败，不得写入新 session owner | reservation 失效并释放 |
| replay | 不重新结算 | 校验 message 的 content、citations 与私有 evidence；不匹配时按无 Citation 公开 | 不创建 token map |

错误处理必须 fail closed：不能验证 `profile`、`session_id`、`stream_id`、`worker_generation`、`turn_key`、`tid`、
settlement owner、工具终态、reference、chunk 或 source 时，都不生成 Citation。失败原因可写安全日志或 turn
journal 诊断，但不得向客户端暴露 MCP 原始敏感内容。

### 6.3 可选资源上限（本期不做）

以下是建议的后续防滥用加固项，不作为首期 Citation 上线的阻塞条件；本期继续沿用现有工具结果 `1MB`、最多
`50` 个 document 等限制：

- 每 turn 的两个 KB 工具总调用次数；
- 每个 stream 的 candidate/token 总数；
- 单 chunk、单 evidence snapshot 与每个 session evidence 的总字节数；
- 每条 assistant message 的最大 Citation/marker 数；
- 单个内部 marker 长度，以及未闭合 `[[c:` 的流式缓冲上限；
- CandidateRegistry / SettlementRegistry 的 TTL、最大 record 数与过期清理指标。

实施这些限制时必须 fail closed：超限项不创建 Citation，但普通工具完成事件与无引用回答仍可继续；不得通过截断
token、ID 或 evidence 后继续结算。TTL 清理也必须遵循 owner/generation 与 terminal lifecycle，不能回收仍处于
`attached_pending` 且正在执行 `s.save()` 的 record。

## 7. 实施边界

### WebUI 仓库

新增 Fork 实现应置于：

```text
integration/knowledge_base/citations.py
```

职责：

- 基于 `turn_references.py` 的规范化结果生成 `reference_id` / `chunk_id`；
- 管理按完整 CandidateScope 隔离的 CandidateRegistry、SettlementRegistry 与 token 单向状态；
- 基于完整、权威 function result 建立 candidate；拒绝 preview/snippet/未知业务成功状态；
- 将知识库 reference 从 public live delta 拆出，保证它不进入 SSE 或 `STREAM_LIVE_MANIFEST`；
- 解析、验证和结算最终 assistant 内部标记；
- 维护所有公开/持久化/诊断出站通道共用的内部 token 过滤投影；
- 创建 `message.citations` 与私有 evidence snapshot；
- 从已引用 snapshot 为 Manifest 做严格 read projection；
- 提供单元测试中的纯函数入口。

现有文件的改动应保持为薄接缝：

| 文件 | 需要的最小改动 |
| --- | --- |
| `integration/knowledge_base/turn_references.py` | 在既有严格解析后保留下游可信不可变 `chunk_id`；缺失时用 `tool + tid + row_index + chunk_index + page_content` 生成 call-scoped fallback；禁止仅按 `page_content` 去重或跨调用复用 fallback。每个 chunk 必须独立进入 candidate。 |
| `integration/session_manifest/manifest.py` | 将 document/chunk ID 投影到 wire；提供复用的 KB/public-live delta 拆分投影；读路径只合并已提交且 identity/digest 校验通过的 citation evidence，并排除当前未结算 turn。 |
| `api/helpers.py` | 薄接缝：调用 integration-owned strip helper，提供唯一的 `public_session_projection()`；所有返回 `session + messages` 的 handler 必须经此入口，再执行现有文本脱敏，禁止直接序列化原始 `messages`。 |
| `api/streaming.py` | 将 WebUI-owned `KnowledgeBaseCitationHook` 注入本轮 Agent；从完整 function result 建 candidate；live merge 前拆出 KB references；在最后 assistant message 已确定后获取 Session 写锁，按 `owner/generation + session_revision + message identity + content digest` 复验并 attach；在同一次 `s.save()` 中持久化 content、citations 和私有 evidence，save 成功后才 commit/consume、递增 revision、清理 owner；所有出站副本复用 token 过滤投影，`done` 使用保存后的 messages。 |
| `api/gateway_chat.py` | 使用同一完整结果、live-delta 拆分、统一出站过滤和 Session 写锁；Gateway 不支持 host citation hook 时保持 fail closed，不能根据 preview 或外部事件猜测引用。 |
| `api/routes.py` 及 session mutation/export 接缝 | 所有结构化 Session 出站响应（包括 duplicate、truncate、workspace/settings 更新、compression/recovery、share create/revoke）复用统一公开投影；任何 handler 禁止直接返回 `s.messages`、`session.messages` 或其深拷贝；写操作复制/截断历史消息时，先按规则重签 Citation，无法重签则原子删除 marker/citations/evidence，再保存并投影响应；Manifest 构建读取原始 Session，但 active stream 合并前再次应用 public-live projection。 |
| `api/shares.py` / transcript export 接缝 | 保留既有窄化字段白名单，并保证任何中间或最终 payload 都不携带私有 evidence。 |

不要在 `api/streaming.py`、`api/gateway_chat.py` 或外部前端重新解析 MCP 原始 JSON；知识库结果解析的唯一实现仍是
`integration/knowledge_base/turn_references.py`。

### Hermes Agent 仓库

Hermes Agent 侧需要接入 `KnowledgeBaseCitationHook`。`agent/tool_executor.py` 的 sequential 与 concurrent 两条
路径继续通过既有 `tool_complete_callback` 把完整结果同步交给 WebUI，使下一次 provider 调用前已有 candidate；新增
接缝位于 `agent/conversation_loop.py` 从 canonical `messages` 构造 provider-facing `api_messages` 的共享投影阶段，
对匹配的 tool message 调用 `KnowledgeBaseCitationHook.annotate_tool_content_for_provider()`。正常的 untrusted wrapper
仍在最外层。

这两个表示必须分离：canonical `messages` 与 `state.db` 保留原 tool result，provider-facing `api_messages` 才
在对应 chunk 对象中含 `_cite` token。Hook 每次从当前 CandidateRegistry 生成 annotation，无需保存第二份工具结果。
`annotate_tool_content_for_provider()` 不负责决定 Citation 是否有效，也不持久化 WebUI 的 Manifest 数据。

同一个 hook 的 `prepare_final_assistant()` 在无 tool-call 的 final assistant message 构造后、
`messages.append(final_msg)` 与任何 state.db flush 前调用。它接收 raw assistant content，将权威 settlement payload
留在 WebUI-owned registry，只返回公开 content 与可选的 opaque settlement ID；Agent 用公开 content 写 final
message/state.db，并通过非持久化结果传回 settlement ID。hook 缺失或异常时，Agent 必须移除内部 marker 而不是把
raw token 落盘。

不要复用 `transform_tool_result` plugin hook：它会改变通用工具结果，且配置插件不是 WebUI 的受信任 host surface。
WebUI 端也不得假设仅凭 system prompt 或 tool completion SSE callback 就能把 token 交给已经运行中的模型。

## 8. 验收测试

至少覆盖下列可观察行为：

1. 单工具返回一个 Document 的两个 chunks、模型只输出其中一个 token：content 只出现一个服务端 marker，Citation
   的 `reference_id` 指向 Manifest 同一 document ID，单元素 `chunk_ids` 只指向被采用的 chunk；另一 chunk 不进入 Citation/evidence。
2. 同一权威命名空间内两个工具命中同一 immutable `document_id`（或受控降级的同名文档）：Manifest 只有一个 reference ID，保留两个真实 source；Citation 可选择其中实际采用的 source/chunk。
3. 同一 Document 的两个不同 chunks 分别支撑两条陈述：模型输出两个不同 token，生成连续 ordinal `1`、`2`；两个
   Citation 可共享 `reference_id`，但各自的单元素 `chunk_ids` 必须不同。
4. 未输出内部 token：即使 Manifest 有 candidates，最终 message 也没有 `citations`。
5. 伪造、未知、跨 Profile/session/stream/generation/turn/tid、失败或 cancelled 工具的 token：可见内部标记被移除，`citations` 不增加。
6. token 指向不存在 chunk、chunk 不属于 reference，或尝试把同一 Document 的其它 chunks 添加到单元素
   `chunk_ids`：拒绝 Citation，且不得通过文本相似度、数组位置或 score 猜测替代 chunk。
7. token 被模型重复输出：每个位置显示同一个 `ordinal`，但只生成一条 Citation/evidence；不同 chunk 的 ordinal
   仍连续。
8. `done`、`GET /api/session`、重放的 session payload 返回相同的 content、`ordinal`、`reference_id` 和单元素 `chunk_ids`。
9. 工具结果因压缩或分页不再完整可见时，已保存 citation evidence 仍能让 Manifest 给出同 ID 的引用详情。
10. error、cancel、worker replacement 和 Gateway 路径都不会留下可跨轮复用的 candidate token 或 settlement reservation。
11. citation internal token 被拆分成多个 Agent 输出 delta 时，外部 `/api/chat/stream` 仍不出现完整或半截
    internal token；`done` 后仅出现已验证的 server-generated `<sup>` marker。
12. Agent sequential / concurrent 工具执行都只在 provider-facing 副本的对应 chunk 对象中加入同一 `tid` 的
    独立 `_cite`，同一 Document 的多个 chunks 各有自己的 token，且并发同名检索不会串用 token。
13. 知识库工具 completed 后，SSE `manifest_delta`、`STREAM_LIVE_MANIFEST`、active-turn Manifest GET 的顶层及
    `turns[].references[]` 均不含本轮 `knowledge_base_document`；历史 turn reference 仍存在。收到 `done` 后以
    `citation.reference_id` 查询完整 Manifest，能够获得同一 document 的详情。WebUI 与 Gateway 两条路径都覆盖。
14. Agent sequential / concurrent 两条路径的 provider-facing API tool message 都只在正确 chunk 对象中包含
    当前 `tid` 的 `_cite`；同一 Document 的其它 chunks 不得共享该 token；canonical `messages`、Agent `state.db`、
    WebUI session、外部 SSE token 与 `done` 均不含**服务端生成的** candidate token（MCP 原始 payload 内的相似字串
    不计入该断言）。
15. Agent 的正常完成、error、cancel、worker replacement 和 teardown 均清理 CandidateRegistry；runtime 未实现
    等价的 provider annotation 与 final settlement 接缝时 fail closed。
16. 含 `[[c:...]]` 的模型 raw final output 在 Agent final state.db flush 前已结算为公开 `<sup>`
    marker；Agent state.db、WebUI Session 与外部 SSE 均无内部 marker，而 WebUI Session 保留 `citations[]`。
17. `/api/chat/stream` done、Gateway done、`GET /api/session`、分页/重放、JSON export 以及所有返回完整 Session
    的 mutation 响应（duplicate、truncate、workspace/settings、compression/recovery、share create/revoke）都保留公开
    `content + citations[]`，且递归断言不存在 `_knowledge_base_citation_evidence`；Markdown/HTML export 与 public
    share 维持各自字段白名单，但同样断言 evidence 不存在。
18. 即使 `api_redact_enabled=false`，上述所有公开响应、日志和诊断 payload 也不包含私有 evidence；证明字段剥离
    独立于可配置的文本脱敏。
19. 同一个持久化原始 Session 经公开投影后不再含 evidence，但 `build_session_manifest()` 读取原始 Session 时仍能
    在完整 tool result 缺失的 fixture 中恢复相同 `reference_id`、`chunk_ids` 和真实 source。
20. 公开投影 helper 不原地修改原始 Session/message；先生成 done 或 `/api/session` 响应后再构建 Manifest，恢复
    结果仍保持不变。
21. 对同一个有效 token，分别只改变 Profile、session ID、stream ID、worker generation、turn key 或 `tid` 的相邻
    fixture 均被拒绝；stale worker 即使持有正确 token，也不能在新 generation 下 attach。
22. `tid` 为空、带首尾空白、超过 256 字符、包含换行/NUL/其它控制字符或不在允许字符集时不创建 Citation
    candidate；普通工具结果处理不受影响。
23. token 在一个 raw final 的多个位置复用同一个 ordinal，整个 settlement 成功后只原子消费一次；把相同
    token 用于第二个 settlement、重放重新结算或并发 settlement 时均被拒绝。
24. 篡改 Agent 返回值中的 `reference_id`、`chunk_ids`、`source.tool/tid`、`citation_id`、`ordinal`、public content
    或目标 message 后，WebUI attach 都依据 host-owned record 拒绝；Agent 返回额外 citations/evidence 也不会落盘。
25. settlement prepare 只成功一部分、attach 校验失败、`s.save()` 失败、cancel 或 teardown 时，reservation 和
    SettlementRegistry record 都被释放/失效，且不会出现已消费 token 对应未持久化 Citation 的半提交状态。
26. `s.save()` 成功前 owner/generation 不会被清理；save 成功后严格按“commit/consume -> 清理 owner -> done”顺序。
27. token 分别出现在 reasoning、interim assistant、后续工具参数、tool preview/result、apperror、partial/cancel
    snapshot、日志和 journal 时，所有公开或诊断副本均不含完整或半截 token；原始 final buffer 仍可正确结算。
28. 只提供 preview/snippet、`is_error=true`、未 completed 或下游业务失败的工具事件不创建 candidate/Citation。
29. 不同租户/账户命名空间的同名 KB + 同名文件不合并；没有可信 namespace 的 document ID 只在
    `profile + session_id` 内稳定。
30. duplicate/branch 已重签证据时可保留历史 Citation；无法重签时，marker、citations 和 evidence 被原子删除；
    edit/retry/truncate/compression 不会遗留失真的 Citation。
31. 同一 Document 返回两个 `page_content` 完全相同但位置不同的 chunks 时，解析器保留各自的
    `row_index/chunk_index`（或可信下游 `chunk_id`），生成不同的 `chunk_id` 和 token；两个不同工具调用即使正文相同，
    在没有可信不可变 chunk ID 时也不得合并 candidate。
32. 对 duplicate、truncate、workspace/settings 更新、compression/recovery、share create/revoke 等每个返回
    `session + messages` 的接口，使用带 evidence、内部 marker 和未知私有字段的 fixture 调用响应；响应递归检查只能
    包含公开投影字段，绝不能出现 `_knowledge_base_citation_evidence`，且不得观察到原始 `s.messages` 的对象引用或未投影副本。
33. 在 Agent 已写入 marker 但 WebUI 尚未保存 `citations[]`/evidence 的窗口强制进程重启；所有 replay、`GET /api/session`、
    done、export 和 Manifest 响应都必须隐藏孤立 marker/citations；只有同一条 message 的 content、citations 和 evidence
    自洽时才可点击引用。
34. 并发执行 attach 与 edit、retry、truncate 或另一个 stream：第二个写入使 `session_revision` 变化时，attach 在同一
    Session 写锁内检测到 revision/content digest 不匹配并 fail closed，不覆盖后写入的 message；无竞态时才按
    “attach -> s.save() -> commit/consume -> 清理 owner -> done”顺序完成。

建议测试位置：

```text
integration/tests/knowledge_base/test_citations.py
integration/tests/knowledge_base/test_turn_references.py
integration/tests/session_manifest/test_knowledge_base_citations.py
tests/test_streaming_knowledge_base_citations.py
tests/test_gateway_chat_knowledge_base_citations.py
```

## 9. 发布与兼容性

这是对 `/api/chat/stream` 的 `done.session.messages[]` 和 Session Manifest reference wire 的加法扩展。
旧客户端会忽略未知字段，继续显示普通文本；新客户端只有在发现 `message.citations` 后才渲染角标。

实施时应将 `docs/api/session-manifest-api.md` 更新为已实施的字段契约，并在其中删除或替换与本提案冲突的
“references 不直接生成聊天区引用”描述。上线前先以 capability/version 字段或服务端配置开关灰度，确保外部
前端不会把内部 token 或未结算 candidate 显示给用户。
