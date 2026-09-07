# 知识库引用角标：外部前端接入说明

> **适用范围：** 消费 `/api/chat/stream`、`GET /api/session` 与
> `GET /api/session/manifest` 的外部前端。
>
> **目标：** 在最终 assistant 回答中渲染知识库引用角标；点击角标后展示其精确对应的文档和 chunk。

本说明只描述外部前端可见的 API 和处理顺序。前端不需要、也不能使用服务端内部的 `_cite`、内部 token 或
`_knowledge_base_citation_evidence`。

## 1. 接入结论

知识库引用只在一轮聊天成功结束后的**最终 assistant message**中可用。前端需要使用三份公开数据：

1. 最终 message 的 `content`：包含可直接渲染的 HTML 角标
   `<sup data-c="N">[N]</sup>`；
2. 同一 message 的 `citations[]`：将 `N` 关联到一个精确 document chunk；
3. `GET /api/session/manifest`：提供文档名、知识库名、chunk 正文和 score。

映射关系固定为：

```text
content 中 <sup data-c="N">[N]</sup>
  -> 同一 message.citations[] 中 ordinal === N 的条目
  -> citation.reference_id
  -> manifest.references[] 中 id === reference_id 的文档
  -> citation.chunk_ids[0]
  -> document.metadata.chunks[] 中 id === chunk_ids[0] 的唯一 chunk
```

不要用文件名、正文相似度或检索顺序进行匹配。

## 2. 流程

```text
POST /api/chat/start
  -> session_id + stream_id
  -> GET /api/chat/stream?stream_id=...
       -> token：仅临时显示流式正文，不处理引用
       -> done：以 done.session.messages 覆盖临时内容
                 -> 获取最终 assistant message.citations[]
                 -> GET /api/session/manifest?session_id=...
                 -> 渲染/点击时按 ordinal 精确定位 chunk
```

### 2.1 流式阶段

`token` 事件只用于临时显示回答正文。知识库角标不会在 token 事件中实时下发，且服务端不会为知识库检索发送
`manifest_delta.references[]`。

因此流式阶段：

- 可以显示普通文本；
- 不要试图从工具事件、`manifest_delta` 或正文内容推断引用；
- 不要在 token 文本末尾手动追加角标。

### 2.2 收到 `done` 后

`done.session.messages` 是本轮持久化后的最终事实来源。前端必须用其中的最终 message **整体覆盖**本次流式临时
message，而不是将 `<sup>` 追加到已经显示的 token 文本后面。

随后请求：

```http
GET /api/session/manifest?session_id={session_id}
```

知识库 document/chunk 只会在最终 Citation 成功保存后出现在该 GET 响应中；不要等待或依赖之前的
`manifest_delta`。

## 3. 最终 message 的公开形状

```json
{
  "role": "assistant",
  "id": 42,
  "content": "规则要求先完成仿真校核。<sup data-c=\"1\">[1]</sup> 同一规则还要求记录校核结果。<sup data-c=\"1\">[1]</sup> 调度支持系统应集中运维。<sup data-c=\"2\">[2]</sup>",
  "citations": [
    {
      "citation_id": "kbcite:v1:7f2a...",
      "ordinal": 1,
      "reference_id": "kbdoc:v1:Fb8...",
      "chunk_ids": ["kbchunk:v1:Q3c..."],
      "source": {
        "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
        "tid": "call_01"
      }
    },
    {
      "citation_id": "kbcite:v1:9a1c...",
      "ordinal": 2,
      "reference_id": "kbdoc:v1:Fb8...",
      "chunk_ids": ["kbchunk:v1:R7d..."],
      "source": {
        "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
        "tid": "call_01"
      }
    }
  ]
}
```

字段规则：

| 字段 | 前端用途 | 规则 |
| --- | --- | --- |
| `id` | 会话内 message 身份 | 与 `session_id` 组合后可用作本地缓存键；不是全局 ID。 |
| `content` | 直接按既有 Markdown + 受限 HTML 策略渲染 | 只有服务端生成的 `<sup data-c="N">[N]</sup>` 是可交互角标。 |
| `citations[]` | Citation 元数据 | 字段缺失或空数组表示此 message 没有已验证的知识库引用。 |
| `ordinal` | 角标关联键 | `data-c` 的十进制值必须与它严格相等。 |
| `reference_id` | 定位 Manifest document | 精确匹配 `manifest.references[].id`。 |
| `chunk_ids` | 定位 Manifest chunk | 当前只含一个元素；用 `chunk_ids[0]` 精确匹配 `metadata.chunks[].id`。 |
| `source` | 可选展示溯源信息 | 是实际采用该 Citation 的 MCP 调用，不要用它匹配文档。 |

### 同一 chunk 多次引用

同一个 chunk 支撑多个陈述时，正文可出现多处同样的角标，例如两处都是 `[1]`。这不是重复数据：

- `citations[]` 只包含一条 `ordinal: 1`；
- 每个 `[1]` 都打开同一个 chunk；
- 不要因为角标在正文出现两次而创建两条引用卡片或重新编号。

不同 chunk 才使用不同序号，按最终 message 中首次出现的顺序从 `1` 连续编号。

### 同一句由多个 chunk 联合支撑

同一个事实性陈述可以同时依赖多个**不同** chunk。此时服务端保留模型标记在该陈述后出现的顺序，生成连续的多个
角标：

```html
该结论需要同时结合适用范围和例外条款理解。<sup data-c="1">[1]</sup><sup data-c="2">[2]</sup>
```

前端应将它们视作同一陈述后的多个独立引用，逐个按 `ordinal` 解析；不需要合成一个 Citation，也不应选择其中一个
替代其它角标。当前协议不为一句话设置固定的不同 chunk 数量上限。

服务端会将同一连续角标组按 `ordinal` 升序规范化。因此，某个旧 chunk 的 `[8]` 与新 chunk 的 `[9]` 同时支撑
当前陈述时，最终正文稳定输出为 `[8][9]`，即使模型内部 token 的出现顺序相反。此排序只作用于连续、仅由空格或
制表符分隔的角标；不会跨越普通正文或换行移动角标。

同一个 chunk 在同一事实后重复出现没有额外语义：服务端会复用同一 `ordinal` 和同一 Citation。模型侧应避免输出
重复 token，以免正文视觉上出现冗余的 `[1][1]`；前端也不得根据相邻角标自行改变编号或合并其对应的 Citation。

## 4. Manifest 中的引用详情

`GET /api/session/manifest` 的相关部分形状如下：

```json
{
  "manifest": {
    "references": [
      {
        "kind": "knowledge_base_document",
        "id": "kbdoc:v1:Fb8...",
        "source": [
          {
            "tool": "mcp__ithink_kb_mcp__searchKnowledgeBaseDocumentsAcross",
            "tid": "call_01"
          }
        ],
        "metadata": {
          "kbName": "share49",
          "fileName": "电力市场运行基本规则.docx",
          "chunks": [
            {
              "id": "kbchunk:v1:Q3c...",
              "page_content": "规则要求完成仿真校核，并记录校核结果。",
              "score": 0.82
            },
            {
              "id": "kbchunk:v1:R7d...",
              "page_content": "调度技术支持系统应集中运维。",
              "score": 0.74
            }
          ]
        }
      }
    ]
  }
}
```

查找规则：

```ts
type Citation = {
  citation_id: string;
  ordinal: number;
  reference_id: string;
  chunk_ids: [string];
  source: { tool: string; tid: string };
};

function resolveCitation(manifest: any, citation: Citation) {
  const document = manifest.references.find(
    (item: any) => item.kind === "knowledge_base_document" && item.id === citation.reference_id,
  );
  const chunk = document?.metadata?.chunks?.find(
    (item: any) => item.id === citation.chunk_ids[0],
  );
  return document && chunk ? { document, chunk } : null;
}
```

同一 document 的 `metadata.chunks[]` 按有效数值 `score` 从大到小返回；相同 score 保持下游结果中的原始顺序，
缺失、非数值或非有限 score 排在最后。provider 注入给模型的同一 document chunks 使用完全相同的排序，因此前端
详情列表与模型优先看到的片段一致。该规则不改变 `citations[].ordinal`，也不按 score 重排顶层 `references[]`。

点击 `<sup data-c="N">[N]</sup>` 时：

1. 将 `N` 解析为正整数；
2. 在当前 message 的 `citations[]` 中查找唯一 `ordinal === N` 的条目；
3. 按上述函数获得 document/chunk；
4. 展示 `kbName`、`fileName`、该 chunk 的 `page_content`、可选 `score`，以及可选的 `source.tool/tid`。

如果任一查找失败，保留不可交互的角标文本或展示“引用详情暂不可用”；可重新拉取一次完整 Manifest。仍失败时不得选择
名称相近的其它 document/chunk 代替。

## 5. 渲染与安全要求

将 `content` 交给现有 Markdown renderer 时，只把下列**完整且已验证**的元素作为可点击引用：

```html
<sup data-c="N">[N]</sup>
```

其中 `N` 必须：

- 为十进制正整数；
- 在同一 message 的 `citations[]` 中有且只有一个相同 `ordinal`；
- 元素文本恰好为 `[N]`，且没有额外属性。

不要让模型正文中的任意 `<sup>`、`data-c`、`[1]` 或 `citation_id` 自动变成可点击引用。若 renderer 不能保留 `sup`
和 `data-c`，可在渲染后根据 `content` 中的服务端 marker 与 `citations[]` 生成自己的角标组件；关联键仍然只能是
`ordinal`。

`citation_id` 是持久化身份与重放去重字段，不是 DOM 属性、显示序号或 Manifest 查询参数。

## 6. 刷新、重连与幂等

| 场景 | 前端行为 |
| --- | --- |
| `done` 重放或 SSE 重连 | 以 `session_id + message.id` 覆盖同一条本地 message；复用服务端的 `content`、`citations[]` 和 `citation_id`，不重新编号。 |
| 页面刷新/历史会话 | 从 `GET /api/session` 的 `messages[]` 读取 message 和 `citations[]`，再请求 Manifest。 |
| 未收到 `manifest_delta` | 正常；知识库引用不通过实时 Manifest delta 下发。直接请求完整 Manifest。 |
| 无 `citations[]` | 当作无已验证引用，不从检索工具卡片、文件名或历史 Manifest 反推角标。 |
| Manifest 暂缺 document/chunk | 保留正文，不做错误映射；重拉一次后仍缺失则报告数据不一致。 |

前端可以按 `(session_id, message.id)` 缓存该 message 的 `citations[]`，按 `session_id` 缓存 Manifest。收到同一会话新的
`done`、刷新会话或用户点击角标前缓存过期时，使用最新 GET Manifest 覆盖旧缓存。

## 7. 最小接入清单

1. 消费 `/api/chat/stream` 的 token，作为临时消息展示。
2. 收到 `done` 后，整体覆盖本轮临时消息。
3. 对最后一条真实 `role: "assistant"` message，读取 `content` 和可选 `citations[]`。
4. 若 `citations[]` 非空，调用 `GET /api/session/manifest?session_id=...`。
5. 使用 `data-c -> ordinal -> reference_id -> chunk_ids[0]` 建立点击映射。
6. 仅展示准确匹配的 Manifest chunk；无法匹配时 fail closed，不猜测替代项。

更完整的字段契约见 [Session Manifest HTTP/SSE 契约](session-manifest-api.md)；服务端实现、候选 token 和持久化细节见
[知识库最终回答 Chunk 级引用契约](knowledge-base-citation-contract.md)。
