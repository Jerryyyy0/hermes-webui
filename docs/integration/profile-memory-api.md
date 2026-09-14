# 助理画像记忆接口

本文档描述 Hermes WebUI 后端提供的 Profile 记忆读取、编辑和时间线接口。接口仅在后端设置 `HERMES_INTEGRATION=1` 时启用；前端不需要读取或传递该环境变量。

## 1. 获取全部记忆

```http
GET /api/integration/profiles/{profile}/memory
```

返回当前 Profile 的 `memories/MEMORY.md` 原文，不读取 `USER.md`，也不根据自由文本推测业务分类。文件不存在时仍返回 `memory` 节点，其中 `content=""`、`updated_at=null`。

```json
{
  "profile": "price-audit",
  "assistant_name": "价格审计助理",
  "description": "跨项目生效的助理记忆，包括用户偏好、工作背景、历史偏好和常用知识",
  "updated_at": "2026-09-12T14:30:25+08:00",
  "memories": {
    "memory": {
      "filename": "MEMORY.md",
      "content": "用户希望回答先给结论\n§\n项目数据库使用 PostgreSQL",
      "updated_at": "2026-09-11T09:20:11+08:00"
    }
  }
}
```

## 2. 编辑记忆

```http
PUT /api/integration/profiles/{profile}/memory
Content-Type: application/json
```

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `target` | string | 是 | 固定为 `memory`，对应 `MEMORY.md` |
| `content` | string | 是 | 文件的完整内容；多条记忆使用独占一行的 `§` 分隔 |

```json
{
  "target": "memory",
  "content": "用户希望回答先给结论\n§\n用户主要使用 Java"
}
```

返回：

```json
{
  "ok": true,
  "profile": "price-audit",
  "target": "memory",
  "filename": "MEMORY.md",
  "content": "用户希望回答先给结论\n§\n用户主要使用 Java",
  "updated_at": "2026-09-12T14:35:10+08:00",
  "events_created": 2
}
```

保存使用 Profile 当前 `config.yaml` 中的 `memory.memory_char_limit`，未配置时为 2200 个字符。相同内容不产生时间线事件；清空文件时，每条原有记忆分别产生一条删除事件。传入 `target=user` 返回 400，且不会修改 `USER.md`。

浏览器在启用 WebUI 密码认证时，PUT 请求沿用现有 CSRF 机制，需要携带 `X-Hermes-CSRF-Token`。

## 3. 获取记忆时间线

```http
GET /api/integration/profiles/{profile}/memory/timeline?page=1&page_size=20
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---:|---:|---|
| `profile` | string | 是 | - | Profile 名称 |
| `page` | integer | 否 | 1 | 页码，从 1 开始 |
| `page_size` | integer | 否 | 20 | 每页条数，范围 1～100 |

只返回 `MEMORY.md` 的事件，并按事件写入时间倒序排列。已有及以后产生的 `USER.md` 事件都会被过滤。新功能上线前不回算历史；不存在事件文件时返回空列表。

```json
{
  "profile": "price-audit",
  "items": [
    {
      "event_id": "mem_evt_0123456789abcdef",
      "target": "memory",
      "event_type": "updated",
      "event_name": "修改记忆",
      "occurred_at": "2026-09-12T14:35:10+08:00",
      "content": "用户希望回答先给结论，再提供详细原因",
      "source": "webui"
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 1,
    "has_more": false
  }
}
```

事件内容口径：

| `event_type` | `event_name` | `content` |
|---|---|---|
| `created` | 形成记忆 | 新形成的内容 |
| `updated` | 修改记忆 | 修改后的内容 |
| `deleted` | 删除记忆 | 删除前的内容 |

`source=agent` 表示 Agent 自动修改 `MEMORY.md`、学习图谱操作或 Agent 管理端重置；`source=webui` 表示通过本接口编辑。直接在服务器上改写 Markdown 文件不生成事件。

## 错误状态

| HTTP 状态 | 场景 |
|---:|---|
| 400 | 参数无效、缺少字段或内容超过当前 Profile 的记忆字符上限 |
| 404 | Profile 不存在 |
| 500 | 记忆文件或时间线读取、保存失败 |
