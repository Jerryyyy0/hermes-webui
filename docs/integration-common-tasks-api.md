# 常办任务 API 文档

Base URL：`http://127.0.0.1:8787`（按实际端口调整）

**范围**：HTTP 接口仅服务岗位助理欢迎页「常办任务」卡片的读取。数据从该 profile 的 WebUI 会话日志中聚类挖掘而来,首次进入或挖掘失败时由 LLM 结合人设/技能生成 seed 兜底。本地存储为 `<profile_home>/common_tasks.db`。

**启用条件**：`HERMES_INTEGRATION=1`。后端单线程 worker 串行处理 seed/cluster 任务,冷却 10min,失败重试 3s。

---

## 1. 获取常办任务列表

**GET** `/api/integration/common_tasks`

### 输入（Query）

| 参数 | 必填 | 类型 | 默认 | 说明 |
|------|------|------|------|------|
| `profile` | 是 | `string` | — | Profile 名称,如 `default`、`ass2`。profile 不存在返回 404 |

### 响应

| 字段 | 类型 | 说明 |
|------|------|------|
| `profile` | `string` | 回显的 profile 名称 |
| `items` | `array<Item>` | Top 3 任务卡片,mined 优先（按 `query_count` DESC）,不足用 seed 补足,仍不足则返回实际数量（可能为空数组） |
| `cache_status` | `string` | 缓存状态:`hit` / `seed` / `empty`（见下） |

#### Item 结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `title` | `string` | 任务标题,≤ 15 字 |
| `description` | `string` | 任务描述,≤ 50 字 |
| `trigger_language` | `string` | 用户发起该任务的自然话术,≤ 30 字。**前端点击卡片时应将此字段填入输入框** |
| `query_count` | `integer` | 问话次数。seed 来源恒为 `0`;mined 来源为该簇内去重后的真实提问次数（≥ 3） |
| `source` | `string` | 数据来源:`seed`（LLM 基于人设/技能生成的兜底）/ `mined`（从真实对话聚类挖掘） |

#### `cache_status` 含义

| 值 | 含义 | 前端建议 |
|----|------|----------|
| `hit` | 至少一条 `source=mined` 的任务 | 正常展示卡片。说明已挖掘出真实高频任务 |
| `seed` | 仅有 `source=seed` 的任务（首次进入 / 对话数不足 / 挖掘冷却中） | 正常展示卡片,但视觉上可标注「推荐」或弱化 `query_count`（seed 恒为 0） |
| `empty` | 任务表为空（worker 尚未跑完首次 seed） | 隐藏卡片区域,或展示骨架屏;几秒后重试 |

### 行为说明

- **异步生成**:首次调用若 DB 为空,本次响应 `cache_status=empty`,同时后端入队 seed 生成任务。几秒后再次调用即可拿到 `seed` 或 `hit`。
- **触发挖掘**:每次调用都会检查 fingerprint（最近 50 条去重问题的 SHA-256）。fingerprint 变化且过冷却期 -> 入队 mine;fingerprint 不变且 10min 内 -> 跳过。
- **冷却**:mine 成功后 10min 内不重挖;mine 失败后 3s 内不重试。
- **并发**:同一 profile 后入队的任务覆盖前入（只跑最新）。

### 成功响应示例

```json
{
  "profile": "default",
  "items": [
    {
      "title": "生成日报大纲",
      "description": "根据工作记录生成日报大纲",
      "trigger_language": "帮我生成今天的日报",
      "query_count": 7,
      "source": "mined"
    },
    {
      "title": "处理PDF文件",
      "description": "提取文本、合并或转换PDF文件",
      "trigger_language": "帮我把这个PDF提取文字",
      "query_count": 5,
      "source": "mined"
    },
    {
      "title": "编辑Google文档",
      "description": "在Google文档中更新或编辑内容",
      "trigger_language": "帮我编辑这个Google文档",
      "query_count": 0,
      "source": "seed"
    }
  ],
  "cache_status": "hit"
}
```

**说明**:`items` 数组里 mined 在前（按 `query_count` DESC）,seed 在后补足 3 条。上例中 2 条 mined + 1 条 seed 混合,`cache_status` 因存在 mined 而为 `hit`。

### 首次进入示例

```json
{
  "profile": "ass2",
  "items": [],
  "cache_status": "empty"
}
```

几秒后再次调用:

```json
{
  "profile": "ass2",
  "items": [
    {
      "title": "代码任务委托",
      "description": "通过 Codex 或 Claude Code 执行编程任务",
      "trigger_language": "帮我写个 Python 脚本",
      "query_count": 0,
      "source": "seed"
    },
    {
      "title": "绘制架构图",
      "description": "生成架构图或流程图并导出 SVG",
      "trigger_language": "画一个系统架构图",
      "query_count": 0,
      "source": "seed"
    },
    {
      "title": "生成信息图",
      "description": "将要点转成信息图或知识漫画",
      "trigger_language": "把这篇内容做成信息图",
      "query_count": 0,
      "source": "seed"
    }
  ],
  "cache_status": "seed"
}
```

### 错误响应

| HTTP 状态 | 响应体 | 触发条件 |
|-----------|--------|----------|
| `400` | `{"error": "profile 为必填参数"}` | 未传 `profile` 或为空 |
| `404` | `{"error": "Profile 不存在"}` | `profile` 名称在 `list_profiles_api()` 中找不到 |
| `500` | `{"error": "<内部错误信息>"}` | 异常未捕获（不应发生） |

错误响应体统一为 `{"error": "<msg>"}` JSON。

---

## 2. 前端集成建议

### 2.1 加载时机

- 在岗位助理欢迎页（compose-footer 或欢迎气泡下方）首次渲染时调用一次
- 切换 profile 时重新调用
- 不需要轮询;用户每次回到欢迎页时调用一次即可,后端会按 fingerprint 自动决定是否挖掘

### 2.2 渲染规则

```javascript
async function loadCommonTasks(profile) {
  const res = await fetch(`/api/integration/common_tasks?profile=${encodeURIComponent(profile)}`);
  if (!res.ok) return [];
  const data = await res.json();
  if (data.cache_status === 'empty') {
    // 隐藏卡片区域或展示骨架屏,1-2s 后重试一次
    setTimeout(() => loadCommonTasks(profile), 1500);
    return [];
  }
  return data.items;
}
```

### 2.3 点击行为

卡片点击 -> 把 `item.trigger_language` 填入输入框（不自动发送,让用户确认）:

```javascript
function onTaskCardClick(item) {
  const input = document.querySelector('#compose-input');
  input.value = item.trigger_language;
  input.focus();
}
```

### 2.4 视觉区分

| source | query_count | 建议样式 |
|--------|-------------|----------|
| `mined` | ≥ 3 | 主样式,可显示 `query_count` 次徽标（如「7 次提问」） |
| `seed` | 0 | 弱化样式（如灰色描边 / 「推荐」角标）,不显示次数 |

---

## 3. 数据来源与生命周期

| 阶段 | 触发 | 数据来源 | 写入字段 |
|------|------|----------|----------|
| 首次进入 | `seed_generated_at` 未设置 | LLM 结合 `display_name`/`description`/`SOUL.md`/技能列表生成 3 条 | `source=seed`, `query_count=0` |
| 真实挖掘 | 去重问题 ≥ 5 条 + fingerprint 变化 + 过冷却 | LLM 对最近 50 条 user 问题聚类,簇内 ≥ 3 条才入表,取 top 3 by `count` | `source=mined`, `query_count=count`, `members_json` 保留簇内原始问题 |
| 挖掘失败 | LLM 返回非法 JSON / 所有簇 < 3 / 调用超时 | 保留旧 mined 行（若有）,设 `retry_after=now+3s` | 不写入新行 |
| Seed 失败 | LLM 调用失败 / model 未配置 | 写 3 条 fallback 占位 + `retry_after=now+3s`,**不设置** `seed_generated_at`（下次仍重试 seed） | `source=seed`, `query_count=0` |

**挖掘数据源**:仅 WebUI 会话（`<profile_home>/sessions/*.json` 中 `role=user` 的消息）,不读 `state.db` 的 cli/cron 行。斜杠命令（`/` 开头）和空文本跳过。

---

## 4. 配置要求

### 4.1 后端

- 环境变量 `HERMES_INTEGRATION=1`
- Profile 必须配置 model（`<profile_home>/config.yaml` 的 `model` 字段）,否则 seed/mine 都走 fallback
- Profile 必须配置对应 provider 的 API key（`<profile_home>/.env` 中,如 `DEEPSEEK_API_KEY=sk-xxx`）

### 4.2 冷却参数（后端常量,前端只读）

| 常量 | 值 | 位置 |
|------|-----|------|
| `SUCCESS_COOLDOWN_SECONDS` | `600` | `integration/common_tasks/generation.py` |
| `FAILURE_RETRY_SECONDS` | `3` | 同上 |
| `MIN_QUESTIONS_FOR_MINING` | `5` | 同上 |
| `TOP_N` | `3` | 同上 |
| 最近问题采样 | `50` | `collectors.collect_recent_user_questions(limit=50)` |

---

## 5. 相关接口

- **助理气泡**:`GET /api/integration/assistant_bubbles?profile=xxx`（同模式,欢迎页气泡文案）
- **Profile 列表**:`GET /api/profiles`（获取所有可用 profile 名称）
