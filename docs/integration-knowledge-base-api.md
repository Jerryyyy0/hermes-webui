# 知识库 API 文档

Hermes WebUI 在 integration 层暴露一组知识库 HTTP 接口，供前端或外部调用方使用。实现见 `integration/knowledge_base/handlers.py`。

## 概述

- **Base path：** `/api/integration/knowledge_base`
- **方法：** 全部为 `POST`
- **身份字段：** `account`、`uuid` 由调用方在请求体中传入；WebUI 不代为查询用户身份



### 调用示例 Base URL

```
http://127.0.0.1:8787
```

完整路径示例：`http://127.0.0.1:8787/api/integration/knowledge_base/list`

---

## 通用约定

### 请求 Content-Type

| 接口 | Content-Type |
|------|--------------|
| 除 `upload_docs` 外 | `application/json` |
| `upload_docs` | `multipart/form-data` |

### 响应

知识库接口的 **HTTP 状态码与 JSON body 原样透传**知识库服务返回值，常见包装格式：

```json
{
  "code": "200",
  "msg": "success",
  "data": {}
}
```

调用方应依据 `code`（及 HTTP 状态码）判断业务是否成功，从 `data` 读取业务数据。部分接口（如检索）也可能直接返回数组或其它 JSON 结构，以实际响应为准。

`show_pdf` 在返回 PDF 时为二进制响应（见第 14 节），非 JSON。

### WebUI 自身错误（非知识库业务错误）

| HTTP | 含义 | Body 示例 |
|------|------|-----------|
| 400 | 请求参数校验失败 | `{"error":"missing_kbName"}` |
| 413 | 上传体超过大小限制 | 纯文本说明 |
| 502 | 知识库服务不可达、未配置或非 JSON 响应 | `{"error":"knowledge_base_upstream_failed","message":"..."}` |
| 404 | 路径不存在或 integration 未启用 | `{"error":"not found"}` |

参数缺失时 `error` 为 `missing_{字段名}`，例如 `missing_account`、`missing_uuid`、`missing_kbNames`。

### 服务端自动补全的默认值

以下字段未在请求中传入时，由 WebUI 自动补全（定义于 `integration/knowledge_base/constants.py`）：

| 字段 | 默认值 | 适用接口 |
|------|--------|---------|
| `vsType` | `"faiss"` | create |
| `embedModel` | `"bce-base"` | create |
| `iconType` | `1` | create / edit |
| `location` | `"101"`（create）/ `101`（edit） | create / edit |
| `size` | `15` | available / members / documents |
| `chunkSize` | `"500"` | upload_docs |
| `chunkOverlap` | `"50"` | upload_docs |
| `deleteContent` | `true` | delete_docs |
| `notRefreshVsCache` | `false` | delete_docs |
| `topK` | `3` | search_docs / search_docs_xcore |
| `scoreThreshold` | `1.0` | search_docs / search_docs_xcore |

---

## 接口列表

| # | 路径 | 说明 |
|---|------|------|
| 1 | `/list` | 知识库列表 |
| 2 | `/joined` | 已加入的团队知识库 |
| 3 | `/create` | 创建知识库 |
| 4 | `/info` | 知识库详情 |
| 5 | `/edit` | 编辑知识库 |
| 6 | `/delete` | 删除知识库 |
| 7 | `/available` | 可加入的团队知识库（分页） |
| 8 | `/apply_join` | 申请加入团队知识库 |
| 9 | `/members` | 知识库成员列表（分页） |
| 10 | `/documents` | 文档列表（分页 + 筛选） |
| 11 | `/upload_docs` | 上传文档（multipart，第一步） |
| 12 | `/update_docs` | 更新文档元数据（第二步） |
| 13 | `/delete_docs` | 删除文档 |
| 14 | `/show_pdf` | 预览 PDF / 文档 |
| 15 | `/search_docs` | 单库检索 |
| 16 | `/search_docs_xcore` | 跨库检索 |

---

## 接口详情

### 1. 知识库列表

`POST /api/integration/knowledge_base/list`

**请求体**

| 字段 | 必填 | 说明 |
|------|------|------|
| `account` | 是 | 账号 |
| `uuid` | 是 | 用户 UUID |
| `isPersonal` | 是 | `1` 个人库，`0` 团队库 |

```json
{
  "account": "admin",
  "uuid": "aaaaaaaa0000aaaa0000aaaaaaaaaaaa",
  "isPersonal": 1
}
```

---

### 2. 已加入的团队知识库

`POST /api/integration/knowledge_base/joined`

| 字段 | 必填 |
|------|------|
| `account` | 是 |
| `uuid` | 是 |

---

### 3. 创建知识库

`POST /api/integration/knowledge_base/create`

| 字段 | 必填 | 说明 |
|------|------|------|
| `account` | 是 | |
| `uuid` | 是 | |
| `showName` | 是 | 显示名称 |
| `isPersonal` | 是 | `1` 个人库，`0` 团队库 |
| `kbIntro` | 否 | 简介，默认 `""` |
| `location` | 否 | 默认 `"101"` |
| `iconType` | 否 | 默认 `1`；请求体显式传入时覆盖 |

创建时服务端自动附带 `vsType=faiss`、`embedModel=bce-base`（调用方无需传入）。

**响应示例（成功，无业务数据）：**

```json
{
  "code": "200",
  "msg": "success",
  "data": null
}
```

---

### 4. 知识库详情

`POST /api/integration/knowledge_base/info`

| 字段 | 必填 |
|------|------|
| `kbName` | 是 |

---

### 5. 编辑知识库

`POST /api/integration/knowledge_base/edit`

| 字段 | 必填 | 说明 |
|------|------|------|
| `kbName` | 是 | |
| `showName` | 是 | |
| `kbIntro` | 否 | 默认 `""` |
| `iconType` | 否 | 默认 `1` |
| `location` | 否 | 默认 `101`（整数） |

---

### 6. 删除知识库

`POST /api/integration/knowledge_base/delete`

| 字段 | 必填 |
|------|------|
| `account` | 是 |
| `kbName` | 是 |

---

### 7. 可加入的团队知识库（分页）

`POST /api/integration/knowledge_base/available`

| 字段 | 必填 | 说明 |
|------|------|------|
| `account` | 是 | |
| `uuid` | 是 | |
| `page` | 是 | 页码 |
| `size` | 是 | 每页条数；未传时用 `15` |
| `showName` | 否 | 筛选，默认 `""` |
| `kbIntro` | 否 | 筛选，默认 `""` |

---

### 8. 申请加入团队知识库

`POST /api/integration/knowledge_base/apply_join`

| 字段 | 必填 |
|------|------|
| `account` | 是 |
| `uuid` | 是 |
| `kbName` | 是 |

---

### 9. 知识库成员列表（分页）

`POST /api/integration/knowledge_base/members`

| 字段 | 必填 | 说明 |
|------|------|------|
| `uuid` | 是 | |
| `kbName` | 是 | |
| `page` | 是 | |
| `size` | 是 | 未传时用 `15` |
| `username` | 否 | 筛选，默认 `""` |
| `account` | 否 | 筛选，默认 `""` |

---

### 10. 文档列表（分页 + 筛选）

`POST /api/integration/knowledge_base/documents`

| 字段 | 必填 | 说明 |
|------|------|------|
| `kbName` | 是 | |
| `page` | 是 | |
| `size` | 是 | 未传时用 `15` |
| `fileLevel` | 否 | 默认 `""` |
| `fileName` | 否 | 默认 `""` |
| `fileNumber` | 否 | 默认 `""` |
| `fileClass` | 否 | 默认 `""` |
| `fileOrg` | 否 | 默认 `""` |
| `fileExt` | 否 | 默认 `""` |
| `status` | 否 | 默认 `[]` |
| `sortName` | 否 | 默认 `""` |
| `sortOrder` | 否 | 默认 `""` |

---

### 11. 上传文档（multipart，第一步）

`POST /api/integration/knowledge_base/upload_docs`

**Content-Type：** `multipart/form-data`

| 表单字段 | 必填 | 说明 |
|---------|------|------|
| `uuid` | 是 | 用户 UUID |
| `kbName` | 是 | 知识库名称 |
| `files` | 是 | 文件二进制，字段名 `files` |
| `fileProperties` | 是 | JSON 数组字符串 |
| `chunkSize` | 否 | 默认 `"500"` |
| `chunkOverlap` | 否 | 默认 `"50"` |

`fileProperties` 示例：

```json
[
  {
    "fileName": "doc.pdf",
    "fileClass": "",
    "fileUploader": "uuid-xxx",
    "publicationDate": "1"
  }
]
```

---

### 12. 更新文档元数据（第二步）

`POST /api/integration/knowledge_base/update_docs`

| 字段 | 必填 | 说明 |
|------|------|------|
| `kbName` | 是 | |
| `fileNames` | 是 | 非空字符串数组 |
| `fileProperties` | 是 | 非空对象数组 |

须在 `upload_docs` 成功之后调用。

---

### 13. 删除文档

`POST /api/integration/knowledge_base/delete_docs`

| 字段 | 必填 | 说明 |
|------|------|------|
| `kbName` | 是 | |
| `fileNames` | 是 | 非空字符串数组 |
| `deleteContent` | 否 | 默认 `true` |
| `notRefreshVsCache` | 否 | 默认 `false` |

---

### 14. 预览 PDF / 文档

`POST /api/integration/knowledge_base/show_pdf`

| 字段 | 必填 | 说明 |
|------|------|------|
| `kbName` | 是 | |
| `fileName` | 是 | |
| `flag` | 否 | docx 转 PDF 预览时传 `true`；仅显式传入时生效 |

PDF 预览：

```json
{
  "kbName": "share54",
  "fileName": "1656号附件-电力中长期市场基本规则.pdf"
}
```

docx 转 PDF：

```json
{
  "kbName": "share54",
  "fileName": "xxx.docx",
  "flag": true
}
```

**响应：**

- 成功且为 PDF：`Content-Type: application/pdf`，body 为 PDF 字节流（可能附带 `Content-Disposition`）
- 其它情况：JSON，格式与知识库服务一致

保存 PDF 示例：

```bash
curl -sS -X POST "$BASE/show_pdf" \
  -H "Content-Type: application/json" \
  -d '{"kbName":"share68","fileName":"doc.pdf"}' \
  -o preview.pdf
```

---

### 15. 单库检索

`POST /api/integration/knowledge_base/search_docs`

| 字段 | 必填 | 说明 |
|------|------|------|
| `query` | 是 | 检索语句 |
| `kbName` | 是 | 知识库名称 |
| `topK` | 否 | 返回条数上限，默认 `3` |
| `scoreThreshold` | 否 | 相关度阈值 `0`–`1`，越小相关度越高，默认 `1.0`，建议 `0.5` |

**响应示例（`data` 为切片数组时）：**

```json
{
  "code": "200",
  "msg": "success",
  "data": [
    {
      "page_content": "文本切片内容...",
      "metadata": {
        "source": "knowledge_base/share20/content/文件名称.pdf",
        "file_name": "文件名称.pdf"
      }
    }
  ]
}
```

部分部署下也可能直接返回数组，以实际响应为准。

```bash
curl -sS -X POST "$BASE/search_docs" \
  -H "Content-Type: application/json" \
  -d '{"query":"党建知识库","kbName":"share46","topK":5,"scoreThreshold":0.5}'
```

---

### 16. 跨库检索

`POST /api/integration/knowledge_base/search_docs_xcore`

| 字段 | 必填 | 说明 |
|------|------|------|
| `query` | 是 | 检索语句 |
| `kbNames` | 是 | 非空字符串数组 |
| `topK` | 否 | 默认 `3` |
| `scoreThreshold` | 否 | 默认 `1.0` |

**响应示例（`data` 对象）：**

```json
{
  "code": "200",
  "msg": "success",
  "data": {
    "kbNames": ["share15"],
    "docNames": ["2025年1月电力交易统计月报.pdf"],
    "context": ["总交易电量完成5541亿千瓦时..."]
  }
}
```

`kbNames`、`docNames`、`context` 为平行数组，同下标表示同一条命中。

```bash
curl -sS -X POST "$BASE/search_docs_xcore" \
  -H "Content-Type: application/json" \
  -d '{"query":"2025年1月电力交易成交电量是多少","kbNames":["share15"],"topK":3,"scoreThreshold":1}'
```

---

## 文档上传流程

须两步串联：

```
upload_docs (multipart 上传文件)
       ↓ 成功
update_docs (JSON 写入 fileProperties)
```

---

## curl 速查

```bash
BASE="http://127.0.0.1:8787/api/integration/knowledge_base"
ACCOUNT="admin"
UUID="aaaaaaaa0000aaaa0000aaaaaaaaaaaa"
KB="share54"

# 个人知识库列表
curl -sS -X POST "$BASE/list" \
  -H "Content-Type: application/json" \
  -d "{\"account\":\"$ACCOUNT\",\"uuid\":\"$UUID\",\"isPersonal\":1}"

# 创建知识库
curl -sS -X POST "$BASE/create" \
  -H "Content-Type: application/json" \
  -d "{\"account\":\"$ACCOUNT\",\"uuid\":\"$UUID\",\"showName\":\"我的知识库\",\"isPersonal\":1}"

# 文档列表
curl -sS -X POST "$BASE/documents" \
  -H "Content-Type: application/json" \
  -d "{\"kbName\":\"$KB\",\"page\":1,\"size\":15}"

# PDF 预览
curl -sS -X POST "$BASE/show_pdf" \
  -H "Content-Type: application/json" \
  -d "{\"kbName\":\"$KB\",\"fileName\":\"doc.pdf\"}" \
  -o preview.pdf

# 上传文档
curl -sS -X POST "$BASE/upload_docs" \
  -F "uuid=$UUID" \
  -F "kbName=$KB" \
  -F 'fileProperties=[{"fileName":"doc.pdf","fileClass":"","fileUploader":"'"$UUID"'","publicationDate":"1"}]' \
  -F "files=@/path/to/doc.pdf"
```

---

## 相关资源

- 实现代码：`integration/knowledge_base/`
- Integration 路由总览：[`integration/README.md`](../integration/README.md)
- OpenAPI（Swagger tag `IntegrationKnowledgeBase`）：[`integration/swagger/openapi.json`](../integration/swagger/openapi.json)
- 在线文档：`GET http://127.0.0.1:8787/docs`

修改接口或配置后需**重启 WebUI** 使变更生效。
