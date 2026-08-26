# 技能上传逻辑与接口说明

本文档梳理 WebUI 本地自定义技能的上传、编辑、删除流程及相关接口。

## 概述

技能上传分为两个阶段：

1. **文件上传**：将 `.md` 或 `.zip` 文件保存到本地技能目录
2. **元数据保存**：调用 LLM 提取结构化信息，生成 `detail.json`

前端在一次"保存"操作中依次调用 `POST /api/skillhub/upload` 和 `POST /api/skillhub/skill/detail`。

## 目录结构

上传后的技能目录结构如下：

```
skills/
└── <category>/              # 有分类时
    └── <leaf>/              # 目录名由表单 name 决定
        ├── SKILL.md         # 技能主文件
        ├── .category        # 分类标记文件
        ├── detail.json      # 结构化元数据（display_name、detail_json 等）
        └── ...              # zip 解压的其他文件
```

无分类时直接为 `skills/<leaf>/`。

## 目录名确定优先级

目标目录名（`leaf`）按以下优先级确定：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | `explicit_dir_name` 参数 | 显式指定的目录路径（如 `my-folder`） |
| 2 | `request_name`（表单 name） | 用户在表单中填写的英文逻辑名 |
| 3 | SKILL.md frontmatter `name` | 文件内嵌的 name 字段 |
| 4 | 上传文件名 stem | 文件名去掉扩展名 |

当表单提供 `name` 时，`logical_name`（用于查找已有同名技能）也统一使用表单 `name`，不再从 frontmatter 解析，避免因 frontmatter name 与已有技能相同而覆盖。

## 覆盖（overwrite）机制

| 场景 | 行为 |
|------|------|
| `overwrite=True` + 已有同名技能 | 删除旧目录，写入新内容 |
| `overwrite=True` + 目标路径冲突 | 也会删除冲突路径 |
| `overwrite=False` + 已有同名技能 | 返回 409 `"技能已存在"` |
| 任何情况下目标是 hub 安装技能 | 返回 409，不可覆盖 |

前端在 scope 为 `custom` 时自动附带 `overwrite=1`。

---

## 接口说明

### 1. POST `/api/skillhub/upload` — 上传技能

上传 `.md` 或 `.zip` 文件到本地技能目录。

multipart 文件上传不设置 WebUI 侧的文件大小上限；若使用 JSON 形式提交
`content`，仍受通用 20 MiB 请求体限制。反向代理或上游网络层仍可能设置更小的限制。

**Content-Type**: `multipart/form-data`

**表单参数**:

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `file` | file | 是 | 上传文件，仅支持 `.md` 和 `.zip` |
| `name` | string | 否 | 技能逻辑名，决定目标目录名 |
| `category` | string | 否 | 技能分类（如 `devops`） |
| `dir_name` | string | 否 | 显式指定目标目录路径 |
| `overwrite` | string | 否 | 是否覆盖，接受 `"1"` / `"true"` / `"yes"` |
| `check_name` | string | 否 | 重名检查用逻辑名 |
| `check_display_name` | string | 否 | 重名检查用中文显示名 |

**重名检查**：`check_name` 或 `check_display_name` 非空时，后端遍历所有自定义技能的 frontmatter 做大小写不敏感比较，重名返回 409。

**成功响应**:

```json
{
  "ok": true,
  "skill_count": 1,
  "file_count": 2,
  "skills": [
    {
      "name": "my-skill",
      "dir_name": "my-skill",
      "category": "devops",
      "custom": true
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `name` | 逻辑名（表单 name 非空时与 `dir_name` 一致） |
| `dir_name` | 相对于 skills 目录的路径 |
| `category` | 分类 |
| `custom` | 固定为 `true` |

**错误响应**:

| 状态码 | 说明 |
|--------|------|
| 400 | 缺少文件、文件格式无效、SKILL.md 格式错误 |
| 409 | 技能已存在（且未传 `overwrite`） |
| 413 | JSON 请求体超过通用限制，或被反向代理/上游网络层拒绝 |

---

### 2. POST `/api/skillhub/extract` — 提取 ZIP 内容

从 ZIP 文件中提取 SKILL.md 内容，不持久化存储。

multipart 上传不设置 WebUI 侧的文件大小上限；反向代理或上游网络层仍可能设置限制。

**Content-Type**: `multipart/form-data`

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `file` | file | 是 | 仅支持 `.zip` |

**响应**:

```json
{
  "content": "---\nname: my-skill\n...",
  "name": "my-skill"
}
```

---

### 3. POST `/api/skillhub/edit` — 编辑已有技能

更新已有自定义技能的 SKILL.md 内容。

**Content-Type**: `application/json`

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `name` | string | 是 | 技能逻辑名 |
| `content` | string | 是 | SKILL.md 新内容 |
| `dir_name` | string | 否 | 目录路径（用于精确定位） |

**约束**：系统技能（bundled）和 hub 安装技能不可编辑（403）。

**响应**:

```json
{
  "ok": true,
  "name": "my-skill",
  "dir_name": "my-skill",
  "category": "devops",
  "custom": true
}
```

---

### 4. POST `/api/skillhub/delete` — 删除技能

删除本地自定义技能目录及其所有文件。

**Content-Type**: `application/json`

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `name` | string | 是 | 技能逻辑名 |
| `dir_name` | string | 否 | 目录路径 |

**响应**:

```json
{
  "ok": true,
  "name": "my-skill",
  "dir_name": "my-skill",
  "hub_installed": false
}
```

---

### 5. POST `/api/skillhub/skill/ai-meta` — LLM 元数据提取

调用大模型从 SKILL.md 内容中提取结构化元数据。采用 3 步 LLM 编排：

1. **Step 1**（条件执行）：当 `name` 或 `description` 为空时，从 SKILL.md 提取英文 name 和 description
2. **Step 2**（并行）：将英文 name/description 翻译为中文 `skillName` / `displayDescription`
3. **Step 3**（并行）：从 SKILL.md 提取结构化详情 `detailJson`

**Content-Type**: `application/json`

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `skillMdContent` | string | 是 | SKILL.md 完整文本 |
| `name` | string | 否 | 已知的英文逻辑名（非空则跳过 Step 1 的 name 提取） |
| `description` | string | 否 | 已知的英文描述（非空则跳过 Step 1 的 description 提取） |

**响应**:

```json
{
  "name": "my-skill",
  "description": "A brief English description",
  "skillName": "我的技能",
  "displayDescription": "一段中文描述",
  "detailJson": {
    "taskGoal": "核心任务目标",
    "taskDetails": ["步骤1", "步骤2"],
    "useMode": "使用说明",
    "triggerKeywords": ["关键词1"],
    "requiredInfo": [{"label": "信息项", "required": true}],
    "dialogExample": {"user": "示例问法", "assistant": "示例回应"}
  }
}
```

**容错**：任何步骤失败返回 `null`，不影响其他字段。LLM 调用超时 60s，JSON 解析支持截断修复和重试。

**模型配置**：读取 `~/.hermes/config.yaml` 中的 `model.provider` 和 `model.default`。

---

### 6. POST `/api/skillhub/skill/detail` — 保存 detail.json

将 AI 提取的元数据保存为技能目录下的 `detail.json` 文件。

**Content-Type**: `application/json`

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `name` | string | 是 | 技能逻辑名 |
| `detail` | object | 是 | 完整的 detail.json 内容 |
| `dir_name` | string | 否 | 目录路径 |

**detail 对象结构**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 英文逻辑名 |
| `description` | string | 英文描述 |
| `display_name` | string | 中文显示名 |
| `display_description` | string | 中文摘要 |
| `category` | string | 分类 |
| `detail_json` | object | 结构化详情（taskGoal、taskDetails 等） |

**响应**:

```json
{
  "ok": true,
  "name": "my-skill"
}
```

---

## 前端上传流程

前端 `submitAiMetaForm` 函数的完整流程：

```
用户选择文件 (.md / .zip)
        │
        ▼
读取文件内容 → 正则提取 frontmatter 中的 name/description
        │
        ▼
POST /api/skillhub/skill/ai-meta
  (skillMdContent, name, description)
        │
        ▼
展示表单：用户编辑 name / display_name / display_description / category
        │
        ▼
前端重名检查（遍历 _skillhubData 比较 name 和 display_name）
        │
        ▼
Step 1: POST /api/skillhub/upload
  FormData: file, name, category, overwrite, check_name, check_display_name
        │
        ▼
Step 2: POST /api/skillhub/skill/detail
  JSON: name, dir_name, detail (含 display_name, display_description, detail_json)
        │
        ▼
刷新技能列表 → 打开技能详情
```

## detail.json 文件格式

保存到技能目录的 `detail.json` 包含顶层元数据和嵌套的结构化详情：

```json
{
  "name": "my-skill",
  "description": "A brief English description",
  "display_name": "我的技能",
  "display_description": "一段中文描述",
  "category": "devops",
  "detail_json": {
    "taskGoal": "核心任务目标",
    "taskDetails": ["步骤1", "步骤2"],
    "useMode": "使用说明",
    "triggerKeywords": ["关键词1", "关键词2"],
    "requiredInfo": [
      {"label": "待分析的 CSV 文件", "required": true}
    ],
    "dialogExample": {
      "user": "帮我分析这份数据",
      "assistant": "好的，正在处理..."
    }
  }
}
```

此文件被以下场景读取：

- 技能详情页显示 `display_name` 作为标题、`display_description` 作为摘要
- 技能列表项显示 `display_name` / `display_description`
- 搜索过滤匹配 `display_name` 和 `display_description`

---

## 技能详情页接口调用说明

技能详情页通过 `openSkillHubItem` 函数加载，并行请求以下三个接口：

### 接口列表

| 接口 | 说明 | scope 参数 |
|------|------|-----------|
| `GET /api/skillhub/content` | 获取 SKILL.md 原文 | 支持 |
| `GET /api/skillhub/structure` | 获取 scripts/references 目录结构 | 支持 |
| `GET /api/skillhub/detail` | 获取上游 SkillHub 元数据（仅未安装市场技能） | 不支持 |
| `GET /api/skillhub/file?path=detail.json` | 获取本地 detail.json 文件 | 支持 |

### 各 Tab 调用方式

#### 1. Catalog（市场目录，scope=hub）

| 接口 | URL |
|------|-----|
| 列表 | `GET /api/skillhub/skills?scope=hub` |
| SKILL.md | `GET /api/skillhub/content?name={name}&scope=hub` |
| 结构 | `GET /api/skillhub/structure?name={name}&scope=hub` |
| 元数据 | `GET /api/skillhub/detail?name={name}`（从上游 SkillHub 获取） |

#### 2. Installed（已安装，scope=installed）

| 接口 | URL |
|------|-----|
| 列表 | `GET /api/skillhub/skills?scope=installed` |
| SKILL.md | `GET /api/skillhub/content?name={name}`（auto 模式，本地优先） |
| 结构 | `GET /api/skillhub/structure?name={name}`（auto 模式，本地优先） |
| 元数据 | `GET /api/skillhub/file?name={name}&path=detail.json`（auto 模式，本地优先） |

#### 3. Not installed（未安装，scope=not_installed）

| 接口 | URL |
|------|-----|
| 列表 | `GET /api/skillhub/skills?scope=not_installed` |
| SKILL.md | `GET /api/skillhub/content?name={name}&scope=hub` |
| 结构 | `GET /api/skillhub/structure?name={name}&scope=hub` |
| 元数据 | `GET /api/skillhub/detail?name={name}`（从上游 SkillHub 获取） |

#### 4. My skills（自定义技能，scope=custom）

| 接口 | URL |
|------|-----|
| 列表 | `GET /api/skillhub/skills?scope=custom` |
| SKILL.md | `GET /api/skillhub/content?name={name}&scope=custom` |
| 结构 | `GET /api/skillhub/structure?name={name}&scope=custom` |
| 元数据 | `GET /api/skillhub/file?name={name}&path=detail.json&scope=custom` |

### scope 参数行为

| scope 值 | 行为 |
|----------|------|
| `custom` | 仅读取本地 `shared_skills_dir`，404 当缺失 |
| `hub` | 仅从上游 SkillHub 获取 |
| `auto`（默认） | 本地优先，本地不存在时回退到上游 |

**注意**：`GET /api/skillhub/detail` 接口不支持 scope 参数，始终从上游 SkillHub 获取。已安装和自定义技能的元数据通过 `GET /api/skillhub/file?path=detail.json` 从本地读取。
