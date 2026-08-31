# Cron 闲时窗口字段接口方案

## 1. 目标与范围

为 `POST /api/integration/crons/create` 增加闲时起止时间信息，并让 Cron 查询接口返回相同字段。第一期中，`idle_window` 是 Cron Hub 保存和展示的**任务扩展元数据**；它不改变 Hermes Agent 的自动调度，也不是检测机器 CPU 是否空闲。

本方案覆盖：

- Cron Hub 的创建、更新和查询契约；
- `jobs.json` 持久化与历史任务兼容；
- OpenAPI 和自动化测试。

不改变任务的 `schedule` 表达式、`next_run_at`、手动运行和 Agent scheduler 的任何现有语义。

## 2. 字段契约

建议将两个时间字段收敛到一个顶层 `idle_window` 对象，避免任务顶层继续增加成组字段：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `idle_window` | `object \| null` | 否 | 任务的闲时起止信息；省略或 `null` 表示未配置。 |
| `idle_window.start_schedule` | `object` | 是（配置时） | 闲时开始边界；复用 Cron schedule 结构。 |
| `idle_window.end_schedule` | `object` | 是（配置时） | 闲时结束边界；复用 Cron schedule 结构。 |

规则：

1. 提供 `idle_window` 对象时必须同时包含 `start_schedule` 和 `end_schedule`；对象内只提供一个返回 400，错误信息使用中文。
2. `idle_window` 省略或为 `null` 表示未配置，服务端持久化为 `null`。
3. 更新请求中显式传 `"idle_window": null` 清除已有窗口；省略 `idle_window` 表示保持原值。对象不接受只更新一个子字段的半成品写法。
4. 两端 schedule 仅支持 `kind=once` 或 `kind=cron`，且 `kind` 必须相同；`display` 可省略，服务端会以 `run_at` 或 `expr` 补齐。
5. `kind=once` 时两端均使用带时区偏移的 ISO 8601 `run_at`，并要求结束时间晚于开始时间；`kind=cron` 时两端均使用非空 `expr`。
6. 第一版只接受嵌套结构；`idle_start_time` / `idle_end_time`、`start_time` / `end_time` 平铺字段不是兼容别名，显式出现时统一返回 400，避免拼写错误被静默忽略。

一次性窗口示例：

```json
{
  "idle_window": {
    "start_schedule": {"kind": "once", "run_at": "2026-09-01T22:00:00+08:00"},
    "end_schedule": {"kind": "once", "run_at": "2026-09-02T06:00:00+08:00"}
  }
}
```

## 3. HTTP 接口设计

### 3.1 创建

`POST /api/integration/crons/create`

请求示例：

```json
{
  "profile": "research",
  "name": "夜间报告",
  "schedule": "0 9 * * *",
  "prompt": "汇总昨天的工作",
  "deliver": "local",
  "idle_window": {
    "start_schedule": {
      "kind": "cron",
      "expr": "0 22 * * *",
      "display": "每天 22:00"
    },
    "end_schedule": {
      "kind": "cron",
      "expr": "0 6 * * *",
      "display": "每天 06:00"
    }
  }
}
```

处理顺序：校验字段配对和格式 → 以原参数调用既有 `cron.jobs.create_job()` → 在同一 Profile store 内调用既有 `update_job()` 写入 `profile`、`idle_window` 等 Cron Hub 扩展字段 → 写入任务所属 Profile 的 `cron/jobs.json` → 使用既有 `_cron_job_for_api()` 返回任务。

兼容性约定：如果创建请求不传 `idle_window`，服务端按 `idle_window: null` 处理；任务完全沿用原有 `schedule` 调度逻辑。成功响应和后续查询统一返回 `idle_window: null`。

成功响应中的 `job` 必须包含：

```json
{
  "ok": true,
  "profile": "research",
  "job": {
    "id": "daily_report",
    "profile": "research",
    "schedule": "0 9 * * *",
    "idle_window": {
      "start_schedule": {
        "kind": "cron",
        "expr": "0 22 * * *",
        "display": "每天 22:00"
      },
      "end_schedule": {
        "kind": "cron",
        "expr": "0 6 * * *",
        "display": "每天 06:00"
      }
    }
  }
}
```

### 3.2 更新

`POST /api/integration/crons/update`

请求示例：

```json
{
  "profile": "research",
  "job_id": "daily_report",
  "idle_window": null
}
```

更新接口将 `idle_window` 加入 integration 的白名单，并按“顶层字段是否存在”区分。`idle_window` 采用**整体替换**语义，不支持只修改对象内一个子字段：

- `idle_window` 不存在：不修改窗口；
- `idle_window` 为 `null`：关闭窗口；
- `idle_window` 为包含两个合法 schedule 的对象：替换窗口；
- 对象缺少任一子字段、两端 kind 不同、一次性结束早于开始，或请求显式出现旧平铺时间字段：返回 400，不写入半成品配置。

显式出现平铺字段时必须在字段归一化前拒绝，不能依赖白名单过滤。否则调用方拼错字段后可能收到成功响应，但窗口实际上没有变化，形成静默配置丢失。

更新后仍返回 `{ "ok": true, "profile": ..., "job": ... }`，其中 `job` 返回最终持久化值。

### 3.3 查询

以下查询均应返回 `idle_window`，字段不存在的旧任务统一补为 `null`，避免调用方需要判断字段是否存在：

- `GET /api/crons?all_profiles=1`（Cron Hub 列表）；
- `GET /api/crons?profile=<name>`（单 Profile 列表）；
- 任何复用 `_cron_job_for_api()` 输出单个任务的 Cron 查询。

列表响应示例：

```json
{
  "all_profiles": true,
  "profiles": [
    {
      "profile": "research",
      "jobs": [
        {
          "id": "daily_report",
          "schedule_display": "0 9 * * *",
          "idle_window": {
            "start_schedule": {"kind": "cron", "expr": "0 22 * * *", "display": "每天 22:00"},
            "end_schedule": {"kind": "cron", "expr": "0 6 * * *", "display": "每天 06:00"}
          },
          "next_run_at": "2026-09-01T22:00:00+08:00"
        }
      ]
    }
  ]
}
```

`next_run_at` 仍完全由原有 `schedule` 计算；`idle_window` 不改变其值，也不影响任何执行入口。

## 4. 代码与文档改动边界

### WebUI 仓库

- `integration/crons/handlers.py`：增加 create/update 字段校验、白名单和显式 `null` 清除语义；在白名单过滤前拒绝旧平铺时间字段；错误文案中文化。

- `api/routes.py` 的 `_cron_job_for_api()` 对历史任务补齐 `idle_window: null`，确保单 Profile 与跨 Profile 查询一致；该函数不参与字段校验或调度判断。
- `integration/swagger/openapi.json`：更新 `CronJob`、`CronJobCreateRequest`、`CronJobUpdateRequest`，写明格式、配对约束和 nullable。
- `integration/assets/hermes_integration_crons.js`：若 Cron Hub 本身需要编辑该信息，创建/编辑表单分别收集开始/结束的 schedule；提交时成组发送对象；清空时发送 `idle_window: null`。这不改变任务调度行为。
- `docs/api/cron-hub-api.md`、`integration/README.md`：同步公开接口字段，并明确其仅为扩展元数据。
- `integration/tests/crons/`：增加 handler、序列化、旧任务兼容和“`idle_window` 不影响 `next_run_at`/执行路径”的回归测试。

无需修改 Hermes Agent：既有 `cron.jobs.update_job()` 以 `{**job, **updates}` 合并并保存任务记录，因此能保留由 WebUI 写入的 `idle_window`；Agent scheduler 不读取该字段，现有执行行为不会变化。

## 5. OpenAPI 字段草案

在 `CronJob`、`CronJobCreateRequest`、`CronJobUpdateRequest` 中加入：

```json
"idle_window": {
  "type": "object",
  "nullable": true,
  "required": ["start_schedule", "end_schedule"],
  "properties": {
    "start_schedule": {
      "$ref": "#/components/schemas/IdleWindowSchedule"
    },
    "end_schedule": {
      "$ref": "#/components/schemas/IdleWindowSchedule"
    }
  },
  "additionalProperties": false,
  "description": "闲时时段信息；null 表示未配置。该字段仅保存和查询，不限制 Cron 自动或手动执行"
}
```

`IdleWindowSchedule` 只允许 `once`（`run_at`）或 `cron`（`expr`）；`display` 仅作展示。OpenAPI 无法表达两端 kind 相同或一次性结束晚于开始，这些规则必须由接口说明和服务端校验保证。

## 6. 验证计划

自动化测试至少覆盖：

1. 创建：无窗口、合法一次性窗口、合法 cron 循环窗口、对象缺少一端、非法 kind、两端 kind 不同、一次性结束不晚于开始、显式传旧平铺字段；
2. 更新：保持不变、替换、`idle_window: null` 清除、半成品对象、任务不存在；
3. 查询：新任务返回值、旧 `jobs.json` 缺字段时返回 `idle_window: null`、跨 Profile 不串值；
4. 兼容：带或不带 `idle_window` 的任务，其 `next_run_at`、自动执行和手动运行路径与修改前一致；
5. HTTP：所有 JSON 响应继续使用 `j()`/`bad()`，响应带 `Content-Length`，4xx 错误可读文案为中文。

本次仅新增方案文档，不修改根目录 `CHANGELOG.md`；真正实现时应按 integration 约定更新 `integration/CHANGELOG.md`，并在 PR 中列出 WebUI 侧验证结果。

## 7. 推荐落地顺序

1. 在 WebUI integration handler 增加字段契约、创建后写入、更新和查询归一化测试；
2. 同步 OpenAPI 与 `docs/api/cron-hub-api.md`；
3. 如 Cron Hub 页面需要编辑该信息，再接入表单和详情展示，验证桌面、窄屏、移动端。
