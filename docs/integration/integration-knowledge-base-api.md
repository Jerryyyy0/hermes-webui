# 知识库 BFF 对外 API 文档

## 基本约定

- Base path：`/api/integration/knowledge_base`
- 方法：下文全部为 `POST`
- 启用条件：`HERMES_INTEGRATION=1` 且已配置 `KNOWLEDGE_BASE_URL`
- 除 `upload_artifacts` 外，请求体、下游 HTTP 状态码和 JSON body 均原样转发；WebUI
  不校验、重命名、筛选或补全 JSON 字段。
- 因此，以下字段是当前已知的下游调用契约；下游新增字段可直接传入。未明确列出
  `data` 子字段的响应，以实际下游 JSON 为准。

**通用 JSON 响应**：大多数接口返回下游原样的 `{code, msg, data}`。`code` 可能为
数字或字符串，调用方应同时检查 HTTP 状态码和 `code`。

| WebUI 自身错误 | HTTP | 响应 |
| --- | --- | --- |
| 下游不可达、未配置、超时或响应不是有效 JSON | 500 | `{"error":"知识库服务异常","message":"知识库服务异常，请稍后重试"}` |
| `upload_artifacts` 本地校验失败 | 400 | 中文 `error` 字段 |

以下 JSON 示例使用：

```bash
BASE="http://127.0.0.1:8787/api/integration/knowledge_base"
kb_post() { curl -sS -X POST "$BASE/$1" -H 'Content-Type: application/json' -d "$2"; }
```

## 接口迁移

旧路径已不再支持。请求方法、Content-Type、请求体和响应体不变，只需将最后一级路径
替换为下游接口名。

| 旧路径 | 新路径 |
| --- | --- |
| `/list` | `/list_ps_knowledge_bases` |
| `/joined` | `/user_joined_shkbs` |
| `/create` | `/create_ps_kb` |
| `/info` | `/show_ps_kb_info` |
| `/edit` | `/edit_kb_information` |
| `/delete` | `/delete_ps_kb` |
| `/available` | `/available_shkbs` |
| `/apply_join` | `/apply_join_shkb` |
| `/members` | `/get_user_inshkb` |
| `/documents` | `/list_knowledge_bases_details` |

## 接口目录

| 分类 | 接口 |
| --- | --- |
| 知识库 | `list_ps_knowledge_bases`、`list_qa_knowledge_bases`、`user_joined_shkbs`、`create_ps_kb`、`show_ps_kb_info`、`edit_kb_information`、`delete_ps_kb`、`available_shkbs`、`apply_join_shkb`、`get_user_inshkb` |
| 文档 | `list_knowledge_bases_details`、`upload_docs`、`update_docs`、`delete_docs`、`show_pdf`、`download_doc`、`upload_artifacts` |
| 检索 | `search_docs`、`search_docs_xcore` |
| 成员与消息 | `creater_handle_application`、`get_joinkb_applications`、`get_user_messages`、`mark_message_read`、`user_exit_shkb`、`remove_from_myshkb`、`delete_readed_message` |

## 知识库接口

### `list_ps_knowledge_bases` — 个人/团队知识库列表

`POST /api/integration/knowledge_base/list_ps_knowledge_bases`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `account` | string | 是 | 账号 |
| `uuid` | string | 是 | 用户 UUID |
| `isPersonal` | integer | 是 | `1` 为个人库，`0` 为团队库 |

响应 `data`：下游知识库列表，原样返回。

```bash
kb_post list_ps_knowledge_bases '{"account":"admin","uuid":"uuid-1","isPersonal":1}'
```

### `list_qa_knowledge_bases` — QA 公开知识库列表

`POST /api/integration/knowledge_base/list_qa_knowledge_bases`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `uuid` | string | 是 | 用户 UUID |
| `location` | string | 是 | 位置标识，例如 `"1000"` |

响应 `data`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kbName` | string[] | 知识库名称，与其他数组按索引对应 |
| `showName` | string[] | 知识库展示名称 |
| `isDefault` | boolean[] | 是否默认知识库 |

```bash
kb_post list_qa_knowledge_bases '{"uuid":"uuid-1","location":"1000"}'
```

### `user_joined_shkbs` — 已加入的团队知识库

`POST /api/integration/knowledge_base/user_joined_shkbs`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `account` | string | 是 | 账号 |
| `uuid` | string | 是 | 用户 UUID |

响应 `data`：下游已加入团队知识库列表，原样返回。

```bash
kb_post user_joined_shkbs '{"account":"admin","uuid":"uuid-1"}'
```

### `create_ps_kb` — 创建知识库

`POST /api/integration/knowledge_base/create_ps_kb`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `account` | string | 是 | 创建账号 |
| `uuid` | string | 是 | 用户 UUID |
| `showName` | string | 是 | 展示名称 |
| `isPersonal` | integer | 是 | `1` 个人库，`0` 团队库 |
| `kbIntro` | string | 否 | 知识库简介 |
| `location` | string/integer | 否 | 位置标识 |
| `iconType` | integer | 否 | 图标类型 |

响应：下游 `{code, msg, data}` 原样返回。

```bash
kb_post create_ps_kb '{"account":"admin","uuid":"uuid-1","showName":"我的知识库","isPersonal":1}'
```

### `show_ps_kb_info` — 知识库详情

`POST /api/integration/knowledge_base/show_ps_kb_info`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kbName` | string | 是 | 知识库名称 |

响应 `data`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kbName` | string | 知识库名称 |
| `showName` | string | 展示名称 |
| `kbIntro` | string | 简介 |
| `createdTime` | string | 创建时间 |
| `iconType` | integer | 图标类型 |
| `vsType` | string | 向量存储类型 |
| `embedModel` | string | 嵌入模型 |
| `fileCount` | integer | 文件数 |
| `edition` | string | 知识库版本 |
| `location` | integer[] | 位置标识列表 |

```bash
kb_post show_ps_kb_info '{"kbName":"share68"}'
```

### `edit_kb_information` — 编辑知识库

`POST /api/integration/knowledge_base/edit_kb_information`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kbName` | string | 是 | 知识库名称 |
| `showName` | string | 是 | 展示名称 |
| `kbIntro` | string | 否 | 简介 |
| `iconType` | integer | 否 | 图标类型 |
| `location` | integer/string | 否 | 位置标识 |

响应：下游 `{code, msg, data}` 原样返回。

```bash
kb_post edit_kb_information '{"kbName":"share68","showName":"新名称"}'
```

### `delete_ps_kb` — 删除知识库

`POST /api/integration/knowledge_base/delete_ps_kb`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `account` | string | 是 | 账号 |
| `kbName` | string | 是 | 知识库名称 |

响应：下游 `{code, msg, data}` 原样返回。

```bash
kb_post delete_ps_kb '{"account":"admin","kbName":"share68"}'
```

### `available_shkbs` — 可加入团队知识库（分页）

`POST /api/integration/knowledge_base/available_shkbs`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `account` | string | 是 | 账号 |
| `uuid` | string | 是 | 用户 UUID |
| `page` | integer | 是 | 页码 |
| `size` | integer | 是 | 每页数量 |
| `showName` | string | 否 | 展示名称筛选 |
| `kbIntro` | string | 否 | 简介筛选 |

响应 `data`：下游分页结果，原样返回。

```bash
kb_post available_shkbs '{"account":"admin","uuid":"uuid-1","page":1,"size":15}'
```

### `apply_join_shkb` — 申请加入团队知识库

`POST /api/integration/knowledge_base/apply_join_shkb`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `account` | string | 是 | 账号 |
| `uuid` | string | 是 | 用户 UUID |
| `kbName` | string | 是 | 目标知识库名称 |

响应：下游 `{code, msg, data}` 原样返回。

```bash
kb_post apply_join_shkb '{"account":"admin","uuid":"uuid-1","kbName":"share68"}'
```

### `get_user_inshkb` — 知识库成员列表（分页）

`POST /api/integration/knowledge_base/get_user_inshkb`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `uuid` | string | 是 | 查询用户 UUID |
| `kbName` | string | 是 | 知识库名称 |
| `page` | integer | 是 | 页码 |
| `size` | integer | 是 | 每页数量 |
| `username` | string | 否 | 用户名筛选 |
| `account` | string | 否 | 账号筛选 |

响应 `data`：下游成员分页结果，原样返回。

```bash
kb_post get_user_inshkb '{"uuid":"uuid-1","kbName":"share68","page":1,"size":15}'
```

## 文档接口

### `list_knowledge_bases_details` — 文档列表（分页与筛选）

`POST /api/integration/knowledge_base/list_knowledge_bases_details`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kbName` | string | 是 | 知识库名称 |
| `page` | integer | 是 | 页码 |
| `size` | integer | 是 | 每页数量 |
| `fileLevel`、`fileName`、`fileNumber`、`fileClass`、`fileOrg`、`fileExt` | string | 否 | 下游文档筛选字段 |
| `status` | array | 否 | 状态筛选 |
| `sortName`、`sortOrder` | string | 否 | 下游排序字段 |

响应 `data`：下游文档分页结果，原样返回。

```bash
kb_post list_knowledge_bases_details '{"kbName":"share68","page":1,"size":15}'
```

### `upload_docs` — 上传文档

`POST /api/integration/knowledge_base/upload_docs`

请求为 `multipart/form-data`，body 与 `Content-Type` 原样转发；WebUI 不校验表单字段。

| 表单字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `uuid` | string | 下游定义 | 用户 UUID |
| `kbName` | string | 下游定义 | 知识库名称 |
| `files` | file，可重复 | 下游定义 | 上传文件 |
| `fileProperties` | JSON 数组字符串 | 下游定义 | 文件属性，如 `fileName`、`fileClass`、`fileUploader`、`publicationDate` |
| `chunkSize`、`chunkOverlap` | string | 否 | 下游分块参数 |

响应：下游 `{code, msg, data}` 原样返回。

```bash
curl -sS -X POST "$BASE/upload_docs" \
  -F 'uuid=uuid-1' -F 'kbName=share68' \
  -F 'fileProperties=[{"fileName":"doc.pdf","fileClass":"","fileUploader":"uuid-1","publicationDate":"1"}]' \
  -F 'files=@/path/to/doc.pdf'
```

### `update_docs` — 更新文档元数据

`POST /api/integration/knowledge_base/update_docs`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kbName` | string | 是 | 知识库名称 |
| `fileNames` | string[] | 是 | 文档文件名 |
| `fileProperties` | object[] | 是 | 与文档对应的属性 |

响应：下游 `{code, msg, data}` 原样返回。通常在 `upload_docs` 成功后调用。

```bash
kb_post update_docs '{"kbName":"share68","fileNames":["doc.pdf"],"fileProperties":[{}]}'
```

### `delete_docs` — 删除文档

`POST /api/integration/knowledge_base/delete_docs`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kbName` | string | 是 | 知识库名称 |
| `fileNames` | string[] | 是 | 待删除文件名 |
| `deleteContent` | boolean | 否 | 是否删除原始内容 |
| `notRefreshVsCache` | boolean | 否 | 是否跳过向量缓存刷新 |

响应：下游 `{code, msg, data}` 原样返回。

```bash
kb_post delete_docs '{"kbName":"share68","fileNames":["doc.pdf"]}'
```

### `show_pdf` — 预览文档

`POST /api/integration/knowledge_base/show_pdf`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kbName` | string | 下游定义 | 知识库名称 |
| `fileName` | string | 下游定义 | 文件名 |
| `flag` | boolean | 否 | 文档转 PDF 预览标记 |
| `doc_id` | string | 否 | 下游文档标识；可直接透传 |

响应：下游为文件时返回二进制及原始 `Content-Type` / `Content-Disposition`；否则返回下游
JSON。下游超时为 180 秒。

```bash
curl -sS -X POST "$BASE/show_pdf" -H 'Content-Type: application/json' \
  -d '{"kbName":"share68","fileName":"doc.pdf"}' -o preview.pdf
```

### `download_doc` — 下载文档

`POST /api/integration/knowledge_base/download_doc`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `knowledge_base_name` | string | 下游定义 | 知识库名称 |
| `file_name` | string | 下游定义 | 文件名 |

响应规则与 `show_pdf` 相同：文件二进制或下游 JSON 原样返回，超时为 180 秒。

```bash
curl -sS -X POST "$BASE/download_doc" -H 'Content-Type: application/json' \
  -d '{"knowledge_base_name":"share68","file_name":"doc.pdf"}' -o doc.pdf
```

### `upload_artifacts` — 上传 workspace 成果文件（WebUI 编排）

`POST /api/integration/knowledge_base/upload_artifacts`

这是唯一不是下游同名转发的接口。WebUI 从受信任 workspace 读取 `paths`，再以 multipart
调用下游 `upload_docs`；它不会自动调用 `update_docs`。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `uuid` | string | 是 | 用户 UUID |
| `kbName` | string | 是 | 知识库名称 |
| `fileProperties` | object[] | 是 | 与 `paths` 一一对应的下游文件属性 |
| `paths` | string[] | 是 | workspace 相对文件路径 |
| `chunkSize`、`chunkOverlap` | string | 否 | 未传时由该本地编排接口使用默认值 |

响应：下游 `upload_docs` 的 `{code, msg, data}` 原样返回。

| 本地错误 | HTTP |
| --- | --- |
| 缺少字段、数组长度不一致、路径越界、文件不存在、文件过大、文件数过多 | 400 |

限制：单文件最大 50 MiB，最多 20 个文件。

```bash
kb_post upload_artifacts '{"uuid":"uuid-1","kbName":"share68","fileProperties":[{"fileName":"report.md"}],"paths":["reports/report.md"]}'
```

## 检索接口

### `search_docs` — 单库检索

`POST /api/integration/knowledge_base/search_docs`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | string | 是 | 检索文本 |
| `kbName` | string | 是 | 知识库名称 |
| `topK` | integer | 否 | 返回命中数 |
| `scoreThreshold` | number | 否 | 下游相关度阈值 |

响应：下游命中文本切片 JSON；可能是 `{code, msg, data}` 或直接数组，原样返回。

```bash
kb_post search_docs '{"query":"查询内容","kbName":"share68","topK":5,"scoreThreshold":0.5}'
```

### `search_docs_xcore` — 跨库检索

`POST /api/integration/knowledge_base/search_docs_xcore`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | string | 是 | 检索文本 |
| `kbNames` | string[] | 是 | 待检索知识库名称 |
| `topK` | integer | 否 | 返回命中数 |
| `scoreThreshold` | number | 否 | 下游相关度阈值 |

响应 `data`：下游通常返回按索引对应的 `kbNames`、`docNames`、`context` 数组；实际 body
原样返回。

```bash
kb_post search_docs_xcore '{"query":"查询内容","kbNames":["share68"],"topK":3,"scoreThreshold":1}'
```

## 成员与消息接口

### `creater_handle_application` — 审批加入申请

`POST /api/integration/knowledge_base/creater_handle_application`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `userId` | string | 下游定义 | 申请用户标识 |
| `uuid` | string | 下游定义 | 申请用户 UUID |
| `kbName` | string | 下游定义 | 知识库名称 |
| `action` | string | 下游定义 | `approve`、`reject` 或 `ignore` |

响应：下游 `{code, msg, data}` 原样返回。

```bash
kb_post creater_handle_application '{"userId":"user-1","uuid":"uuid-1","kbName":"share68","action":"approve"}'
```

### `get_joinkb_applications` — 获取加入申请列表

`POST /api/integration/knowledge_base/get_joinkb_applications`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `userId` | string | 下游定义 | 查询用户标识 |
| `uuid` | string | 下游定义 | 用户 UUID |
| `kbName` | string | 下游定义 | 知识库名称 |

响应 `data`：下游申请列表，原样返回。

```bash
kb_post get_joinkb_applications '{"userId":"user-1","uuid":"uuid-1","kbName":"share68"}'
```

### `get_user_messages` — 获取用户消息

`POST /api/integration/knowledge_base/get_user_messages`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `account` | string | 是 | 账号 |
| `uuid` | string | 是 | 用户 UUID |
| `readType` | string | 是 | `all`、`unread` 或 `seen` |

响应 `data`：下游消息列表，原样返回。

```bash
kb_post get_user_messages '{"account":"admin","uuid":"uuid-1","readType":"all"}'
```

### `mark_message_read` — 标记消息已读

`POST /api/integration/knowledge_base/mark_message_read`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `messageId` | integer[] | 下游定义 | 消息 ID 数组 |

响应：下游 `{code, msg, data}` 原样返回。

```bash
kb_post mark_message_read '{"messageId":[1,2]}'
```

### `user_exit_shkb` — 用户退出团队知识库

`POST /api/integration/knowledge_base/user_exit_shkb`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `account` | string | 下游定义 | 账号 |
| `uuid` | string | 下游定义 | 用户 UUID |
| `kbName` | string | 下游定义 | 知识库名称 |

响应：下游 `{code, msg, data}` 原样返回。

```bash
kb_post user_exit_shkb '{"account":"admin","uuid":"uuid-1","kbName":"share68"}'
```

### `remove_from_myshkb` — 创建者移除成员

`POST /api/integration/knowledge_base/remove_from_myshkb`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `account` | string[] | 下游定义 | 待移除账号数组 |
| `uuid` | string[] | 下游定义 | 待移除 UUID 数组 |
| `kbName` | string | 下游定义 | 知识库名称 |

响应：下游 `{code, msg, data}` 原样返回。

```bash
kb_post remove_from_myshkb '{"account":["user-1"],"uuid":["uuid-1"],"kbName":"share68"}'
```

### `delete_readed_message` — 删除已读消息

`POST /api/integration/knowledge_base/delete_readed_message`

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `messageId` | integer[] | 下游定义 | 已读消息 ID 数组 |

响应：下游 `{code, msg, data}` 原样返回。

```bash
kb_post delete_readed_message '{"messageId":[1,2]}'
```

## 维护边界

本文件是对外详细说明的唯一 Markdown 入口。`integration/README.md` 只保留启用方式，
路由对齐方案和迁移通知只作为历史记录；Swagger 的 `IntegrationKnowledgeBase` tag 与
本文件保持路由、方法和 WebUI 自身异常语义一致。
