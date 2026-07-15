# 技能安装/卸载/管理关联助理接口说明

本文档说明技能市场新增的三个接口：批量安装到多个助理、从所有助理卸载技能、同步技能关联的助理。

## 背景

原有接口 `POST /api/skillhub/install` 和 `POST /api/skillhub/delete` 仅操作默认 profile（`shared_skills_dir`）。
新接口支持将技能安装到用户选择的多个助理，从所有助理卸载，以及管理已安装技能的关联助理。

Profile 与技能的关联是文件系统层面的：每个 profile 拥有独立的 `{home}/skills/` 目录。

---

## 接口说明

### 1. POST `/api/skillhub/install-to-profiles` — 批量安装到指定助理

将技能安装到用户选择的多个助理的 skills 目录，默认启用。

**Request Body**:

```json
{
  "name": "skill-name",
  "display_name": "显示名称",
  "category": "category",
  "profiles": ["default", "researcher", "writer"]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 技能逻辑名（上游 catalog 中的 name） |
| `display_name` | string | 否 | 显示名称，缺省使用 name |
| `category` | string | 否 | 分类，缺省时从上游 detail 补齐 |
| `profiles` | string[] | 是 | 目标 profile 名称列表 |

**成功响应** (200):

```json
{
  "ok": true,
  "results": [
    { "profile": "default", "ok": true, "dir_name": "category/skill-name" },
    { "profile": "researcher", "ok": true, "dir_name": "category/skill-name" },
    { "profile": "writer", "ok": false, "error": "Skill already installed" }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `results[].profile` | 目标 profile 名称 |
| `results[].ok` | 是否安装成功 |
| `results[].dir_name` | 安装后的相对路径（成功时） |
| `results[].error` | 失败原因（失败时） |

**错误码**:

| 状态码 | 说明 |
|--------|------|
| 400 | `name` 为空或 `profiles` 非数组 |
| 502 | 上游 SkillHub 请求失败 |
| 503 | SkillHub 未配置 |

**行为说明**:
- 安装到每个 profile 的 `skills/<category>/<name>/` 目录
- 已存在的技能返回 409（`"Skill already installed"`），不影响其他 profile 的安装
- 安装后自动从 profile 的 `config.yaml` 的 `skills.disabled` 列表中移除该技能（默认启用）
- 同时写入 `.hub_installed`、`.install_name`、`.hub_catalog_name`、`detail.json` 等元数据

---

### 2. POST `/api/skillhub/delete-from-all-profiles` — 从所有助理卸载

遍历所有 profile，删除指定技能。

**Request Body**:

```json
{
  "name": "skill-name",
  "dir_name": "category/skill-name"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 技能逻辑名 |
| `dir_name` | string | 否 | 精确目录路径（可选，用于消除歧义） |

**成功响应** (200):

```json
{
  "ok": true,
  "results": [
    { "profile": "default", "ok": true },
    { "profile": "researcher", "ok": true },
    { "profile": "writer", "ok": true, "skipped": true }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `results[].profile` | profile 名称 |
| `results[].ok` | 是否成功（未找到也算成功，`skipped=true`） |
| `results[].skipped` | 该 profile 不存在此技能，跳过 |
| `results[].error` | 失败原因（失败时） |

**错误码**:

| 状态码 | 说明 |
|--------|------|
| 400 | `name` 为空 |
| 403 | 系统技能不可删除 |
| 502 | 内部错误 |

**行为说明**:
- 遍历所有 profile（通过 `list_profiles_api()` 获取）
- 删除每个 profile 的 `skills/` 目录下对应技能文件夹
- 清理 `no_self_improve` 锁定列表
- 清理各 profile 的 `config.yaml` 中 `skills.disabled` 列表
- 不存在该技能的 profile 自动跳过，不报错

---

### 3. POST `/api/skillhub/sync-profiles` — 同步技能关联的助理

一次性完成安装到新助理 + 从旧助理卸载，用于"管理助理"功能。支持市场技能和自建技能。

**Request Body**:

```json
{
  "name": "skill-name",
  "display_name": "显示名称",
  "category": "category",
  "is_custom": false,
  "install": ["profile-a", "profile-b"],
  "uninstall": ["profile-c"]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 技能逻辑名 |
| `display_name` | string | 否 | 显示名称 |
| `category` | string | 否 | 分类，缺省时从上游 detail 补齐 |
| `is_custom` | boolean | 否 | 是否为自建技能（默认 `false`） |
| `install` | string[] | 否 | 需要安装此技能的 profile 列表 |
| `uninstall` | string[] | 否 | 需要卸载此技能的 profile 列表 |

**成功响应** (200):

```json
{
  "ok": true,
  "installed": [
    { "profile": "profile-a", "ok": true },
    { "profile": "profile-b", "ok": true }
  ],
  "uninstalled": [
    { "profile": "profile-c", "ok": true }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `installed[].profile` | 安装操作的目标 profile |
| `installed[].ok` | 是否成功 |
| `installed[].skipped` | 已存在则跳过（`ok=true, skipped=true`） |
| `installed[].error` | 失败原因 |
| `uninstalled[].profile` | 卸载操作的目标 profile |
| `uninstalled[].ok` | 是否成功 |
| `uninstalled[].skipped` | 不存在则跳过 |
| `uninstalled[].error` | 失败原因 |

**错误码**:

| 状态码 | 说明 |
|--------|------|
| 400 | `name` 为空、`install`/`uninstall` 非数组 |
| 502 | 上游 SkillHub 请求失败 |

**行为说明**:
- 先执行 `install` 列表的安装，再执行 `uninstall` 列表的卸载
- **市场技能**（`is_custom=false`）：安装复用 `install_skill_to_profile()`，从上游下载
- **自建技能**（`is_custom=true`）：安装复用 `copy_custom_skill_to_profile()`，从源 profile 复制文件
- 已存在则跳过（`ok=true, skipped=true`）
- 卸载复用 `delete_skill_from_profile()`，不存在则跳过
- 单个 profile 的失败不影响其他 profile 的操作

---

## 前端调用流程

### 安装流程

```
用户点击"安装"按钮
  → _showInstallProfileDialog() 弹窗
  → 异步加载 /api/profiles 获取所有助理列表
  → 显示 checkbox 卡片列表（已安装的灰显禁用）
  → 用户勾选助理，点击"安装"
  → POST /api/skillhub/install-to-profiles
  → 显示 toast 成功/失败提示
  → 刷新技能列表
```

### 卸载流程

```
用户点击"卸载"按钮
  → showConfirmDialog() 确认弹窗
  → POST /api/skillhub/delete-from-all-profiles
  → 显示 toast 提示
  → 刷新技能列表
```

### 管理关联助理流程

```
用户点击"管理助理"按钮（市场技能和自建技能均已安装时显示）
  → _showManageProfilesDialog() 弹窗
  → 异步加载 /api/profiles 获取所有助理列表及各自的 skills
  → 显示 checkbox 卡片列表，已关联的助理默认勾选（可取消）
  → 用户调整勾选，点击"保存"
  → 前端计算 diff：新增勾选 → install，取消勾选 → uninstall
  → 前端检测 is_custom 标志
  → POST /api/skillhub/sync-profiles（携带 is_custom 参数）
  → 后端根据 is_custom 选择安装方式：
     - 市场技能：从上游 SkillHub 下载
     - 自建技能：从源 profile 复制文件
  → 显示 toast 成功/失败提示
  → 刷新技能列表和技能详情
```

**弹窗行为**:
- 所有助理均可勾选/取消勾选（无禁用状态）
- 全选/取消全选按钮
- 确认时自动计算 diff，只发送变更部分
- ESC / 点击遮罩 / 取消按钮均可关闭

**自建技能复制机制**:
- 函数：`copy_custom_skill_to_profile(name, profile_name, category)`
- 从任意 profile 的 skills 目录中查找源技能（通过 `_find_skill_in_any_profile`）
- 使用 `shutil.copytree` 复制整个技能目录到目标 profile
- 自动确保目标 profile 的 config.yaml 中该技能未被禁用

---

## 与原有接口的关系

| 接口 | 作用域 | 说明 |
|------|--------|------|
| `POST /api/skillhub/install` | 默认 profile | 原有接口，仅安装到 `shared_skills_dir` |
| `POST /api/skillhub/install-to-profiles` | 指定 profiles | 新接口，安装到用户选择的多个 profile |
| `POST /api/skillhub/delete` | 默认 profile | 原有接口，仅从默认 profile 删除 |
| `POST /api/skillhub/delete-from-all-profiles` | 所有 profile | 新接口，从所有 profile 删除 |
| `POST /api/skillhub/sync-profiles` | 指定 profiles | 新接口，同步安装+卸载，用于管理助理 |

原有接口保留不变，新接口在前端安装/卸载/管理流程中替代原有接口。

---

## 批量接口

### 4. POST `/api/skillhub/batch-install` — 批量安装多个技能到指定助理

将多个技能一次性安装到用户选择的助理列表。支持市场技能和自建技能混合操作。

**Request Body**:

```json
{
  "skills": [
    {
      "name": "skill-a",
      "display_name": "技能A",
      "category": "devops",
      "is_custom": false
    },
    {
      "name": "my-custom-skill",
      "display_name": "自建技能",
      "category": "",
      "is_custom": true
    }
  ],
  "profiles": ["default", "researcher", "writer"]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `skills` | object[] | 是 | 技能列表（非空） |
| `skills[].name` | string | 是 | 技能逻辑名 |
| `skills[].display_name` | string | 否 | 显示名称 |
| `skills[].category` | string | 否 | 分类，市场技能缺省时从上游 detail 补齐 |
| `skills[].is_custom` | boolean | 否 | 是否为自建技能（默认 `false`） |
| `profiles` | string[] | 是 | 目标 profile 名称列表（非空） |

**成功响应** (200):

```json
{
  "ok": true,
  "results": [
    { "name": "skill-a", "profile": "default", "ok": true, "dir_name": "devops/skill-a" },
    { "name": "skill-a", "profile": "researcher", "ok": true, "dir_name": "devops/skill-a" },
    { "name": "skill-a", "profile": "writer", "ok": true, "skipped": true },
    { "name": "my-custom-skill", "profile": "default", "ok": true, "dir_name": "my-custom-skill" },
    { "name": "my-custom-skill", "profile": "researcher", "ok": true, "dir_name": "my-custom-skill" }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `results[].name` | 技能名称 |
| `results[].profile` | 目标 profile 名称 |
| `results[].ok` | 是否成功 |
| `results[].dir_name` | 安装后的相对路径（成功时） |
| `results[].skipped` | 已存在则跳过（`ok=true, skipped=true`） |
| `results[].error` | 失败原因（失败时） |

**错误码**:

| 状态码 | 说明 |
|--------|------|
| 400 | `skills` 或 `profiles` 为空或非数组 |
| 503 | SkillHub 未配置 |

**行为说明**:
- 双层遍历：外层 skills × 内层 profiles，逐个安装
- 市场技能（`is_custom=false`）：调用 `install_skill_to_profile()`，从上游下载
- 自建技能（`is_custom=true`）：调用 `copy_custom_skill_to_profile()`，从源 profile 复制文件
- 市场技能 category 缺省时自动从上游 `fetch_skill_detail()` 补齐（每个技能只查询一次）
- 已存在的技能返回 `ok=true, skipped=true`，不影响其他安装操作
- 单个安装失败不影响其他 skill × profile 组合

---

### 5. POST `/api/skillhub/batch-uninstall` — 批量从所有助理卸载多个技能

遍历所有 profile，删除指定的多个技能。

**Request Body**:

```json
{
  "skills": [
    { "name": "skill-a", "dir_name": "devops/skill-a" },
    { "name": "my-custom-skill" }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `skills` | object[] | 是 | 技能列表（非空） |
| `skills[].name` | string | 是 | 技能逻辑名 |
| `skills[].dir_name` | string | 否 | 精确目录路径（可选，用于消除歧义） |

**成功响应** (200):

```json
{
  "ok": true,
  "results": [
    {
      "name": "skill-a",
      "results": [
        { "profile": "default", "ok": true },
        { "profile": "researcher", "ok": true },
        { "profile": "writer", "ok": true, "skipped": true }
      ]
    },
    {
      "name": "my-custom-skill",
      "results": [
        { "profile": "default", "ok": true },
        { "profile": "researcher", "ok": true }
      ]
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `results[].name` | 技能名称 |
| `results[].results` | 该技能在各 profile 的卸载结果 |
| `results[].results[].profile` | profile 名称 |
| `results[].results[].ok` | 是否成功（未找到也算成功，`skipped=true`） |
| `results[].results[].skipped` | 该 profile 不存在此技能，跳过 |
| `results[].results[].error` | 失败原因 |
| `results[].error` | 整个技能卸载过程的错误（异常时） |

**错误码**:

| 状态码 | 说明 |
|--------|------|
| 400 | `skills` 为空或非数组 |
| 403 | 系统技能不可删除 |
| 502 | 内部错误 |

**行为说明**:
- 遍历 skills 列表，每个技能调用 `delete_skill_from_all_profiles()`
- `delete_skill_from_all_profiles` 内部遍历所有 profile 并逐个删除
- 不存在该技能的 profile 自动跳过（`ok=true, skipped=true`）
- 系统技能（`.system` 标记）会被拒绝删除
- 单个技能的失败不影响其他技能的卸载

---

## 批量接口前端调用流程

### 批量安装流程

```
用户点击 "Batch" 按钮进入批量模式
  → 技能列表每项左侧显示 checkbox
  → 用户勾选多个技能
  → 点击底部操作栏 "Batch Install"
  → 弹出 _showInstallProfileDialog（跳过已安装过滤，标题显示技能数量）
  → 用户勾选目标助理，点击"安装"
  → POST /api/skillhub/batch-install（携带 skills 数组 + profiles 数组）
  → 显示 toast 成功/失败/部分失败提示
  → 退出批量模式，刷新技能列表
```

### 批量卸载流程

```
用户点击 "Batch" 按钮进入批量模式
  → 技能列表每项左侧显示 checkbox
  → 用户勾选多个技能
  → 点击底部操作栏 "Batch Uninstall"
  → showConfirmDialog 确认弹窗（显示技能数量）
  → POST /api/skillhub/batch-uninstall（携带 skills 数组）
  → 显示 toast 成功/失败提示
  → 退出批量模式，刷新技能列表
```

**批量模式 UI 行为**:
- "Batch" 按钮位于搜索栏右侧，点击切换批量模式开关
- 批量模式下列表项左侧出现 checkbox，默认未选中
- 列表底部显示操作栏：选中计数 + 批量安装 + 批量卸载 + 取消
- 切换 scope 或 category 时自动退出批量模式并清空选择
- "取消"按钮退出批量模式

---

## 完整接口对照表

| 接口 | 作用域 | 技能数 | Profile 数 | 说明 |
|------|--------|--------|------------|------|
| `POST /api/skillhub/install` | 默认 profile | 1 | 1 | 原有，仅安装到 shared_skills_dir |
| `POST /api/skillhub/install-to-profiles` | 指定 profiles | 1 | N | 安装单个技能到多个 profile |
| `POST /api/skillhub/delete` | 默认 profile | 1 | 1 | 原有，仅从默认 profile 删除 |
| `POST /api/skillhub/delete-from-all-profiles` | 所有 profile | 1 | 全部 | 单个技能从所有 profile 删除 |
| `POST /api/skillhub/sync-profiles` | 指定 profiles | 1 | N | 同步安装+卸载，用于管理助理 |
| `POST /api/skillhub/batch-install` | 指定 profiles | N | M | **批量**安装多个技能到多个 profile |
| `POST /api/skillhub/batch-uninstall` | 所有 profile | N | 全部 | **批量**从所有 profile 卸载多个技能 |
