# 通知系统 API 文档

Base URL：`http://127.0.0.1:8787`（按实际端口调整）

**范围**：HTTP 接口仅服务知识库通知（`kb_apply`），数据从下游 `get_user_messages` 实时聚合。`notifications.db` 表结构预留，当前 HTTP 层不读写本地存储。

**启用条件**：`HERMES_INTEGRATION=1` 且配置 `KNOWLEDGE_BASE_URL`（知识库服务根地址）。

审批类操作请调用知识库 BFF（见 [§6](#6-知识库-bff-接口)）。

---

## 公共数据结构

### Notification（列表项）

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `string` | `kb:{下游消息id}` |
| `category` | `string` | 恒为 `kb_apply` |
| `title` | `string` | 标题（来自下游 `massage`） |
| `body` | `string` | 正文（当前为空） |
| `source` | `string` | 来源（下游 `showName`） |
| `ref_id` | `string` | 下游消息 id |
| `status` | `string` | `unread` / `read`；由 WebUI 根据 `read_type` 推导（见下），非下游单条直出字段 |
| `action_status` | `string` | 仅 `massType=1`：由下游 `state` 映射（见 §6.1）：`0`→`rejected`、`1`→`approved`、`2`→`pending`；其余为 `""` |
| `actionable` | `integer` | 是否可打开详情：`massType=1`（知识库加入申请，库主视角）为 `1`；`2`/`3`/`4` 为 `0` |
| `metadata` | `object` | 下游原始消息（含 `massType`、`state` 等） |
| `created_at` | `number` | Unix 时间戳（秒） |
| `updated_at` | `number` | Unix 时间戳（秒） |

#### 下游 `massType`（消息类型）

下游 `get_user_messages` 的 `massType` 为整数，表示知识库相关通知的四种类型：

| `massType` | 含义 | 典型接收方 | WebUI `actionable` | WebUI `action_status` |
|------------|------|------------|--------------------|-----------------------|
| `1` | 知识库加入申请 | 知识库创建者（库主） | `1` | 由 `state` 映射（见下） |
| `2` | 退出知识库 | 知识库创建者 | `0` | `""` |
| `3` | 被拒绝 / 通过加入知识库（申请结果） | 申请人 | `0` | `""`（结果见 `metadata.state`） |
| `4` | 被踢出知识库 | 被踢出成员 | `0` | `""` |

`state` 字段：`0` 拒绝 / `1` 通过 / `2` 默认（待处理）。`massType=1` 时 WebUI 将其映射为 `action_status`：`0`→`rejected`、`1`→`approved`、`2`→`pending`。`massType=3` 时 `state` 表示申请结果（`0` 拒绝、`1` 通过），但不写入归一化后的 `action_status`。

#### `status`（已读/未读）推导规则

下游 `get_user_messages` **无单条已读字段**，仅能通过 `readType` 过滤。WebUI 归一化时按列表查询的 `read_type` 设置 `status`：

| `read_type` | `status` |
|-------------|----------|
| `unread` | 恒为 `unread` |
| `seen` | 恒为 `read` |
| `all` | 额外拉取 `readType=seen` 的消息 ID 集合；ID 在集合中 → `read`，否则 → `unread` |

**已读/删除写入路径**：

| 操作 | 入口 | 下游 |
|------|------|------|
| 标已读 | `POST /notifications/read` | `mark_message_read` |
| 删除 | `POST /notifications/delete` | `delete_readed_message` |
| 通过/拒绝/忽略 | `creater_handle_application` | 下游同步标已读，无需再调 read |

**前端按钮规则**（由 `actionable` / `action_status` / `metadata.massType` 推导，API 不下发 `actions`）：

| 条件 | 展示操作 |
|------|----------|
| `massType=1` 且 `action_status=pending` | 通过 / 拒绝 / 忽略 / 标为已读 |
| 其余 | 标为已读 / 删除 |

审批走 [§6.2](#62-creater_handle_application)；已读走 [§3](#3-标记已读)（下游 `mark_message_read`）；删除走 [§4](#4-删除通知)。

---

## 1. 获取通知列表

**GET** `/api/integration/notifications`

### 输入（Query）

| 参数 | 必填 | 类型 | 默认 | 说明 |
|------|------|------|------|------|
| `account` | 是 | `string` | | 用户账号 |
| `uuid` | 是 | `string` | | 用户 UUID |
| `read_type` | 否 | `string` | `all` | 下游 `readType`：`all` / `unread` / `seen` |
| `action_status` | 否 | `string` | | 逗号分隔，如 `pending` 或 `approved,rejected` |
| `limit` | 否 | `integer` | `20` | 每页条数 |
| `cursor` | 否 | `float` | | 上一页 `next_cursor` |

**永久排除**：列表与摘要均不返回 `massType=3` 且 `state=2` 的申请人「结果待处理」消息（如「您申请加入 … 知识库的结果待处理」）。其余类型（已通过/被拒绝、被移出、库主待审批等）照常返回；无查询参数可重新包含被排除条目。

`read_type=all`（默认）时，WebUI 会额外请求下游 `readType=seen` 以推导每条 `status`。

### 输出（JSON）

| 字段 | 类型 | 说明 |
|------|------|------|
| `items` | `Notification[]` | 通知列表 |
| `next_cursor` | `number \| null` | 下一页游标 |

```json
{
  "items": [
    {
      "id": "kb:97",
      "category": "kb_apply",
      "title": "用户 chenyuxin 申请加入您的 学习资料 知识库",
      "action_status": "pending",
      "actionable": 1,
      "metadata": { "massType": 1, "targetKbName": "share20", "state": 2 },
      "created_at": 1719393016.017132
    }
  ],
  "next_cursor": null
}
```

### curl

```bash
curl -sS \
  'http://127.0.0.1:8787/api/integration/notifications?account=admin&uuid=aaaaaaaa0000aaaa0000aaaaaaaaaaaa&limit=20'

curl -sS \
  'http://127.0.0.1:8787/api/integration/notifications?account=admin&uuid=aaaaaaaa0000aaaa0000aaaaaaaaaaaa&read_type=unread&action_status=pending'

curl -sS \
  'http://127.0.0.1:8787/api/integration/notifications?account=admin&uuid=aaaaaaaa0000aaaa0000aaaaaaaaaaaa&read_type=seen'
```

**返回示例：**

```json
{
  "items": [
    {
      "id": "kb:97",
      "category": "kb_apply",
      "title": "用户 chenyuxin (chenyuxin) 申请加入您的 学习资料 知识库",
      "body": "",
      "source": "学习资料",
      "ref_id": "97",
      "status": "unread",
      "priority": "normal",
      "actionable": 1,
      "action_status": "pending",
      "metadata": {
        "id": 97,
        "massType": 1,
        "massage": "用户 chenyuxin (chenyuxin) 申请加入您的 学习资料 知识库",
        "targetKbName": "share20",
        "targetUserId": "cb2853a32333beb3336dfcb308ba4d",
        "state": 2,
        "showName": "学习资料",
        "createTime": "2026-06-26T14:30:16.017132"
      },
      "created_at": 1782455416.017132,
      "updated_at": 1782455416.017132
    },
    {
      "id": "kb:100",
      "category": "kb_apply",
      "title": "您申请加入 学习资料 知识库的申请已通过",
      "body": "",
      "source": "学习资料",
      "ref_id": "100",
      "status": "unread",
      "priority": "normal",
      "actionable": 0,
      "action_status": "",
      "metadata": {
        "id": 100,
        "massType": 3,
        "massage": "您申请加入 学习资料 知识库的申请已通过",
        "targetKbName": "share20",
        "state": 1,
        "createTime": "2026-06-30T16:33:06.253961",
        "showName": "学习资料"
      },
      "created_at": 1782801186.253961,
      "updated_at": 1782801186.253961
    }
  ],
  "next_cursor": null
}
```

`action_status=pending` 时 `items` 仅含 `action_status` 为 `pending` 的 `massType=1` 条目。

---

## 2. 获取通知摘要

**GET** `/api/integration/notifications/summary`

### 输入（Query）

| 参数 | 必填 | 类型 | 默认 | 说明 |
|------|------|------|------|------|
| `account` | 是 | `string` | | 用户账号 |
| `uuid` | 是 | `string` | | 用户 UUID |
| `read_type` | 否 | `string` | `unread` | 下游 `readType` |

计数规则与 [§1](#1-获取通知列表) 列表一致：永久排除 `massType=3` 且 `state=2` 的「结果待处理」消息。

### 输出（JSON）

| 字段 | 类型 | 说明 |
|------|------|------|
| `total` | `integer` | 通知总数 |
| `unread` | `integer` | 未读总数 |
| `by_category` | `object` | 仅含 `kb_apply` |

### curl

```bash
curl -sS \
  'http://127.0.0.1:8787/api/integration/notifications/summary?account=admin&uuid=aaaaaaaa0000aaaa0000aaaaaaaaaaaa'
```

**返回示例：**

```json
{
  "total": 3,
  "unread": 3,
  "by_category": {
    "kb_apply": {
      "total": 3,
      "unread": 3
    }
  }
}
```

---

## 3. 标记已读

**POST** `/api/integration/notifications/read`

Content-Type：`application/json`

将 `kb:` 前缀 ID 剥离后批量转发到下游 [§6.3 mark_message_read](#63-mark_message_read)（`messageId` 整数数组）。`account` / `uuid` 为调用方必填，用于 WebUI 层校验，不写入下游已读请求体。

### 输入（Body）

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ids` | 是 | `string[]` | 通知 ID 列表（仅处理 `kb:` 前缀）；单条也传数组，如 `["kb:97"]` |
| `account` | 是 | `string` | 用户账号 |
| `uuid` | 是 | `string` | 用户 UUID |

### 输出（JSON）

```json
{ "ok": true, "updated": 1 }
```

### curl

```bash
curl -sS -X POST \
  'http://127.0.0.1:8787/api/integration/notifications/read' \
  -H 'Content-Type: application/json' \
  -d '{
    "ids": ["kb:97"],
    "account": "admin",
    "uuid": "aaaaaaaa0000aaaa0000aaaaaaaaaaaa"
  }'
```

**返回示例：**

```json
{
  "ok": true,
  "updated": 1
}
```

---

## 4. 删除通知

**POST** `/api/integration/notifications/delete`

Content-Type：`application/json`

将 `kb:` 前缀 ID 剥离后批量转发到下游 [§6.4 delete_readed_message](#64-delete_readed_message)（`messageId` 整数数组）。`account` / `uuid` 为调用方必填，用于 WebUI 层校验，不写入下游删除请求体。

### 输入（Body）

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `ids` | 是 | `string[]` | 通知 ID 列表（仅处理 `kb:` 前缀）；单条也传数组 |
| `account` | 是 | `string` | 用户账号 |
| `uuid` | 是 | `string` | 用户 UUID |

### 输出（JSON）

```json
{ "ok": true, "deleted": 1 }
```

### curl

```bash
curl -sS -X POST \
  'http://127.0.0.1:8787/api/integration/notifications/delete' \
  -H 'Content-Type: application/json' \
  -d '{
    "ids": ["kb:97"],
    "account": "admin",
    "uuid": "aaaaaaaa0000aaaa0000aaaaaaaaaaaa"
  }'
```

**返回示例：**

```json
{
  "ok": true,
  "deleted": 1
}
```

---

## 5. 审批操作

调用 [§6.2 creater_handle_application](#62-creater_handle_application)，`action` 取 `approve` / `reject` / `ignore`。

下游在审批成功时会**同步将消息标为已读**，无需再调用 [§3](#3-标记已读)。

---

## 6. 知识库 BFF 接口

BFF 路径：`POST /api/integration/knowledge_base/<route>`  
下游路径：`POST {KNOWLEDGE_BASE_URL}/knowledge_base/<route>`  
Content-Type：`application/json`

### 6.1 get_user_messages

**POST** `/api/integration/knowledge_base/get_user_messages`

#### 输入（Body）

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `account` | 是 | `string` | 用户账号 |
| `uuid` | 是 | `string` | 用户 UUID |
| `readType` | 是 | `string` | `all` / `unread` / `seen`（下游无单条已读字段，以此过滤） |
| `limit` | 否 | `integer` | 条数上限 |
| `before` | 否 | `number` | 游标时间戳 |

#### 输出（JSON）

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | `number` | 业务码 |
| `msg` | `string` | 说明 |
| `data` | `array` | 消息列表 |

`data[]` 元素：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `number` | 消息 ID |
| `massType` | `number` | 消息类型：`1` 知识库加入申请 / `2` 退出知识库 / `3` 被拒绝或通过加入知识库（申请结果） / `4` 被踢出知识库 |
| `massage` | `string` | 消息正文 |
| `showName` | `string` | 知识库展示名 |
| `targetKbName` | `string` | 知识库标识名 |
| `state` | `number` | 拒绝/通过状态：`0` 拒绝 / `1` 通过 / `2` 默认（待处理）。`massType=1` 时映射为 `action_status`（`0`→`rejected`、`1`→`approved`、`2`→`pending`）；`massType=3` 时表示申请结果（`0` 拒绝、`1` 通过），不归一化到 `action_status` |
| `response` | `number` | 下游扩展字段（与 `state` 配合，以知识库服务为准） |
| `createTime` | `string` | 创建时间（ISO） |

```json
{
  "code": 200,
  "msg": "查询成功",
  "data": [
    {
      "id": 97,
      "massType": 1,
      "massage": "用户 chenyuxin 申请加入您的 学习资料 知识库",
      "showName": "学习资料",
      "targetKbName": "share20",
      "state": 2,
      "createTime": "2026-06-26T14:30:16.017132"
    }
  ]
}
```

#### curl

```bash
curl -sS -X POST \
  'http://127.0.0.1:8787/api/integration/knowledge_base/get_user_messages' \
  -H 'Content-Type: application/json' \
  -d '{
    "account": "admin",
    "uuid": "aaaaaaaa0000aaaa0000aaaaaaaaaaaa",
    "readType": "unread",
    "limit": 20
  }'
```

**返回示例：**

```json
{
  "code": 200,
  "msg": "查询成功",
  "data": [
    {
      "id": 97,
      "UserId": "aaaaaaaa0000aaaa0000aaaaaaaaaaaa",
      "massType": 1,
      "massage": "用户 chenyuxin (chenyuxin) 申请加入您的 学习资料 知识库",
      "targetKbName": "share20",
      "targetUserId": "cb2853a32333beb3336dfcb308ba4d",
      "state": 2,
      "response": 0,
      "createTime": "2026-06-26T14:30:16.017132",
      "showName": "学习资料"
    },
    {
      "id": 67,
      "UserId": "aaaaaaaa0000aaaa0000aaaaaaaaaaaa",
      "massType": 2,
      "massage": "用户 田佩佩 (tianpeipei) 已主动退出您的 测试上传文件 知识库",
      "targetKbName": "share78",
      "targetUserId": "30052a0857b348f4b50467d1ba94fb18",
      "state": 2,
      "response": 0,
      "createTime": "2026-02-03T16:42:36.227037",
      "showName": "测试上传文件"
    },
    {
      "id": 88,
      "UserId": "aaaaaaaa0000aaaa0000aaaaaaaaaaaa",
      "massType": 4,
      "massage": "您已被移出 学习资料 知识库",
      "targetKbName": "share20",
      "targetUserId": "cb2853a32333beb3336dfcb308ba4d",
      "state": 2,
      "response": 0,
      "createTime": "2026-03-10T10:00:00.000000",
      "showName": "学习资料"
    }
  ]
}
```

### 6.2 creater_handle_application

**POST** `/api/integration/knowledge_base/creater_handle_application`

#### 输入（Body）

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `account` | 是 | `string` | 操作用户账号 |
| `userId` | 是 | `string` | 申请人用户 ID |
| `uuid` | 是 | `string` | 申请人 UUID |
| `kbName` | 是 | `string` | 知识库标识名 |
| `messageId` | 是 | `string` | 消息 ID |
| `action` | 是 | `string` | `approve` / `reject` / `ignore`（标已读请用 [§6.3](#63-mark_message_read)；删除请用 [§6.4](#64-delete_readed_message)） |

#### 输出（JSON）

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | `number` | 业务码 |
| `msg` | `string` | 说明 |

```json
{
  "code": 200,
  "msg": "操作成功"
}
```

#### curl

```bash
# 通过申请
curl -sS -X POST \
  'http://127.0.0.1:8787/api/integration/knowledge_base/creater_handle_application' \
  -H 'Content-Type: application/json' \
  -d '{
    "account": "admin",
    "userId": "cb2853a32333beb3336dfcb308ba4d",
    "uuid": "cb2853a32333beb3336dfcb308ba4d",
    "kbName": "share20",
    "messageId": "97",
    "action": "approve"
  }'

# 拒绝申请
curl -sS -X POST \
  'http://127.0.0.1:8787/api/integration/knowledge_base/creater_handle_application' \
  -H 'Content-Type: application/json' \
  -d '{
    "account": "admin",
    "userId": "cb2853a32333beb3336dfcb308ba4d",
    "uuid": "cb2853a32333beb3336dfcb308ba4d",
    "kbName": "share20",
    "messageId": "97",
    "action": "reject"
  }'
```

**返回示例：**

```json
{
  "code": 200,
  "msg": "操作成功"
}
```

### 6.3 mark_message_read

**POST** `/api/integration/knowledge_base/mark_message_read`

通知「标记已读」经 [§3](#3-标记已读) 聚合后调用此下游接口。

#### 输入（Body）

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `messageId` | 是 | `integer[]` | 消息 ID 列表 |

#### 输出（JSON）

```json
{
  "code": 200,
  "msg": "操作成功",
  "data": null
}
```

#### curl

```bash
curl -sS -X POST \
  'http://127.0.0.1:8787/api/integration/knowledge_base/mark_message_read' \
  -H 'Content-Type: application/json' \
  -d '{"messageId": [112]}'
```

### 6.4 delete_readed_message

**POST** `/api/integration/knowledge_base/delete_readed_message`

通知「删除」经 [§4](#4-删除通知) 聚合后调用此下游接口。

#### 输入（Body）

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `messageId` | 是 | `integer[]` | 消息 ID 列表 |

#### 输出（JSON）

```json
{
  "code": 200,
  "msg": "操作成功",
  "data": null
}
```

#### curl

```bash
curl -sS -X POST \
  'http://127.0.0.1:8787/api/integration/knowledge_base/delete_readed_message' \
  -H 'Content-Type: application/json' \
  -d '{"messageId": [116, 1]}'
```

