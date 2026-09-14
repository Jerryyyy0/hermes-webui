# 助理主页后端接口

本文档描述 Hermes WebUI 后端提供的助理主页接口。`profile` 表示当前用户实例中的助理 Profile 名称。

> 后端部署前置条件：运行 Hermes WebUI 的容器需要设置环境变量 `HERMES_INTEGRATION=1`。该变量由 Hermes-X 创建用户实例时注入，前端不需要设置、读取或随请求传递。前端只需把当前助理的 Profile 名称放入接口路径。

## Hermes-X 数据依赖

Hermes-X 物化 Profile 时，需要在现有 `info.json` 中增加 `time`：

```json
{
  "display_name": "价格审计助理",
  "description": "专注于物资价格审计的 AI 助理。",
  "logo": "data:image/png;base64,...",
  "welcome": "",
  "time": "2026-06-02T10:20:30+08:00"
}
```

字段要求：

- 路径为 `info.time`，现有 `GET /api/profiles` 会原样返回该字段。
- 使用 ISO 8601，精确到秒并带时区偏移，例如 `2026-06-02T10:20:30+08:00`。
- WebUI 用它作为助理创建/入职时间，并按 Asia/Shanghai 的自然日计算在岗天数。
- 为兼容新旧物化数据，字段缺失或格式无效时主页接口仍返回 200，其中 `created_at=null`、`work_days=0`。

## 1. 获取主页聚合数据

```http
GET /api/integration/profiles/{profile}/overview
```

示例：

```http
GET /api/integration/profiles/price-audit/overview
```

响应示例：

```json
{
  "profile": {
    "name": "price-audit",
    "assistant_name": "价格审计助理",
    "avatar": "data:image/png;base64,...",
    "role_type": "position",
    "status": "online",
    "created_at": "2026-06-02T10:20:30+08:00",
    "description": "专注于物资价格审计的 AI 助理。"
  },
  "metrics": {
    "work_days": 102,
    "automatic_task_count": 5,
    "conversation_task_count": 86
  },
  "activity": {
    "start_date": "2025-09-13",
    "end_date": "2026-09-12",
    "timezone": "Asia/Shanghai",
    "week_starts_on": "monday",
    "user_anchor": 8.0,
    "safe_buffer": 1.6,
    "colors": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
    "days": [
      {"date": "2025-09-13", "count": 0, "ratio": 0.0, "level": 0},
      {"date": "2025-09-14", "count": 3, "ratio": 0.63093, "level": 3}
    ],
    "weeks": [
      {
        "week_start": "2025-09-08",
        "month": "2025-09",
        "days": [null, null, null, null, null, {"date": "2025-09-13", "count": 0, "ratio": 0.0, "level": 0}, {"date": "2025-09-14", "count": 3, "ratio": 0.63093, "level": 3}]
      }
    ],
    "month_labels": [
      {"month": "2025-09", "label": "9月", "week_index": 0}
    ]
  },
  "recent_learned_skills": [
    {
      "name": "document-processing",
      "display_name": "文档处理",
      "description": "处理办公文档",
      "learned_at": "2026-09-12T09:30:00+08:00",
      "disabled": false,
      "source": "skillhub"
    }
  ],
  "raw_files": [
    {
      "file_type": "soul",
      "filename": "SOUL.md",
      "size": 328,
      "updated_at": "2026-09-10T16:20:00+08:00"
    }
  ]
}
```

### 字段口径

- `role_type`：系统内置助理为 `general`，其他助理为 `position`。
- `status`：当前版本固定为 `online`。
- `work_days`：今天日期减 `info.time` 的上海日期，同一天为 0。
- `automatic_task_count`：当前 Profile 下状态为待执行（`scheduled`）或执行中（`running`）的 Cron Job 数；停用、已完成和失败任务不统计，删除后减少。
- `conversation_task_count`：当前 Profile 下仍存在的普通 WebUI 会话数。Cron、Webhook、消息平台、CLI、API 和子 Agent 会话不计入。
- `activity.days[].count`：当天普通 WebUI 会话和 Cron 会话中，最终持久化且可展示的 assistant 正文回复总数。用户、系统、工具、推理、空正文、错误回复、流式中间片段不计入。
- `user_anchor`：当前用户与当前 Profile 在返回区间内所有非零日 `count` 的第 80 百分位。
- `recent_learned_skills`：今天及之前 6 个自然日内最新 3 个技能，包含禁用技能。来源为 SkillHub 安装或 Agent 记忆演化；手工上传与系统内置技能不返回。升级和重新安装以最新 `SKILL.md` 修改时间为准。

### 活跃度等级

对每天的 `count` 执行：

```text
log_count  = ln(count + 1)
clipped    = min(log_count, user_anchor * 1.6)
ratio      = min(clipped / ln(user_anchor + 1), 1)
```

无非零日时 `user_anchor=0`，所有日期 `ratio=0`、`level=0`。等级规则：

| level | ratio |
|------:|-------|
| 0 | `ratio == 0` |
| 1 | `0 < ratio <= 0.20` |
| 2 | `0.20 < ratio <= 0.45` |
| 3 | `0.45 < ratio <= 0.70` |
| 4 | `ratio > 0.70` |

日期范围为今天向前一年且不包含去年同日。例如今天是 2026-09-12，返回 2025-09-13 至 2026-09-12。`weeks` 固定按周一至周日，每周 `days` 固定 7 项，范围外边界使用 `null`。由于滚动范围与星期不固定，可能出现 53 个周列。跨月完整周归到占 4 天或以上的月份；`month_labels[].week_index` 是月份标签对应的周列下标。

## 2. 列出原始档案

```http
GET /api/integration/profiles/{profile}/raw_files
```

仅返回实际存在的固定文件：

| `file_type` | 文件路径 |
|-------------|----------|
| `soul` | `<profile-home>/SOUL.md` |
| `memory` | `<profile-home>/memories/MEMORY.md` |
| `user` | `<profile-home>/memories/USER.md` |

响应：

```json
{
  "profile": "price-audit",
  "files": [
    {"file_type": "soul", "filename": "SOUL.md", "size": 328, "updated_at": "2026-09-10T16:20:00+08:00"}
  ]
}
```

## 3. 预览原始档案

```http
GET /api/integration/profiles/{profile}/raw_files/{file_type}
```

响应中的 `content` 是 UTF-8 Markdown 原文：

```json
{
  "profile": "price-audit",
  "file_type": "soul",
  "filename": "SOUL.md",
  "content": "# 价格审计助理\n..."
}
```

## 4. 下载原始档案

```http
GET /api/integration/profiles/{profile}/raw_files/{file_type}/download
```

成功时返回 `text/markdown; charset=utf-8`，并使用 `Content-Disposition: attachment` 触发下载。

## 错误状态

| HTTP 状态 | 场景 |
|-----------|------|
| 400 | 缺少 Profile 或 `file_type` 不在固定白名单 |
| 404 | Profile 不存在或对应档案不存在 |
| 500 | 档案存在但读取失败 |
