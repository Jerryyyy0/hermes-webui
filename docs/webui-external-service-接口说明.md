# WebUI 调用外部服务接口说明

本文档梳理 **hermes-webui-original** 如何调用 **hermes-external-service**，逐接口说明调用路径、具体逻辑、相对改动前的差异，以及 external-service 侧的实现。

---

## 1. 架构总览

WebUI 与外部服务之间存在 **两条调用路径**：

| 路径 | 调用方 | 入口 | 适用接口 |
|------|--------|------|---------|
| **后端代理** | WebUI Python 后端 | `api/routes.py` → `_ext_service_request()` | Skills / SkillHub 全部接口 |
| **浏览器直连** | 前端 JS | `static/workspace.js` → `apiExternal()` | 仅 `/api/profiles` |

```
浏览器
  │
  ├─ api() ──────────────────► WebUI :8787 /api/*
  │                               │
  │                               ├─ HERMES_EXTERNAL_SERVICE 已配置
  │                               │     └─► external-service :18788
  │                               │             ├─► 本地 HERMES_RUNTIME_DIR
  │                               │             └─► 上游 SKILLHUB_URL
  │                               │
  │                               └─ 未配置 fallback
  │                                     ├─► 本地 skills 目录扫描
  │                                     └─► 直连 SKILLHUB_URL
  │
  └─ apiExternal() ──────────► external-service :18788 /api/profiles
                                   （需 HERMES_EXTERNAL_SERVICE_PUBLIC）
```

### 1.1 环境变量

| 变量 | 作用 | 读取位置 |
|------|------|---------|
| `HERMES_EXTERNAL_SERVICE` | WebUI **后端**调用外部服务的地址 | `api/routes.py` → `_ext_service_base_url()` |
| `HERMES_EXTERNAL_SERVICE_PUBLIC` | 注入前端 `externalServiceUrl`，供浏览器直连 | `api/routes.py` → `_ext_service_public_url()` |
| `HERMES_RUNTIME_DIR` | 外部服务读写 runtime 数据的根目录 | `hermes-external-service/config.py` |
| `SKILLHUB_URL` | 外部服务向上游 SkillHub 请求的地址 | `hermes-external-service/config.py` |

### 1.2 公共逻辑

#### profile 参数解析（后端代理专用）

文件：`hermes-webui-original/api/routes.py` → `_ext_service_profile_param()`

优先级：

1. URL query `?profile=xxx`（前端显式传入）
2. Cookie 中的当前 profile
3. 兜底 `"DEFAULT"`（对应 `{HERMES_RUNTIME_DIR}/skills`）

#### 外部服务 profile 路径映射

文件：`hermes-external-service/config.py` → `get_local_skills_dir()`

| profile 值 | skills 目录 |
|-----------|------------|
| `DEFAULT` | `{HERMES_RUNTIME_DIR}/skills` |
| 其他（如 `abc`） | `{HERMES_RUNTIME_DIR}/profiles/abc/skills` |

---

## 2. 接口清单速查

| # | WebUI 路由 | 方法 | 调用路径 | 外部服务路由 | 前端调用函数 |
|---|-----------|------|---------|-------------|-------------|
| 1 | `/api/profiles` | GET | 浏览器直连 | `/api/profiles` | `apiExternal()` |
| 2 | `/api/skills` | GET | 后端代理 | `/api/skills` | `api()` |
| 3 | `/api/skills/content` | GET | 后端代理 | `/api/skills/content` | `api()` |
| 4 | `/api/skills/save` | POST | 后端代理 | `/api/skills/save` | `api()` |
| 5 | `/api/skills/delete` | POST | 后端代理 | `/api/skills/delete` | `api()` |
| 6 | `/api/skillhub/skills` | GET | 后端代理 | `/api/skillhub/skills` | `api()` |
| 7 | `/api/skillhub/categories` | GET | 后端代理 | `/api/skillhub/categories` | 未使用 |
| 8 | `/api/skillhub/content` | GET | 后端代理 | `/api/skillhub/content` | `api()` |
| 9 | `/api/skillhub/file` | GET | 后端代理 | `/api/skillhub/file` | 未使用 |
| 10 | `/api/skillhub/install` | POST | 后端代理 | `/api/skillhub/install` | `api()` |
| 11 | `/api/skillhub/uninstall` | POST | 后端代理 | `/api/skillhub/uninstall` | `api()` |
| 12 | `/api/skillhub/structure` | — | **未接通** | `/api/skillhub/structure` | 未使用 |

> WebUI 本地仍保留 `GET /api/profiles`（`api/profiles.py`），供 profile 切换等内部逻辑使用，但**前端展示已改走外部服务**。

---

## 3. 逐接口详细说明

---

### 3.1 GET `/api/profiles` — Profile 列表（含 logo）

**调用路径：浏览器直连外部服务**

| 层级 | 文件 | 函数/路由 |
|------|------|----------|
| 前端 | `static/workspace.js` | `apiExternal('/api/profiles')` |
| 前端调用点 | `static/panels.js` | `loadCronProfiles()`、`loadProfilesPanel()`、`toggleProfileDropdown()`、`_kanbanLoadProfileNames()` |
| 外部服务 | `routers/profiles.py` | `get_profiles()` |

#### 调用流程

```
panels.js
  └─ apiExternal('/api/profiles')
       └─ fetch(HERMES_EXTERNAL_SERVICE_PUBLIC + '/api/profiles')
            └─ external-service GET /api/profiles
                 ├─ config.get_all_profiles()     扫描 HERMES_RUNTIME_DIR
                 └─ _read_info_json(profile_path) 读 info.json → logo Base64
```

#### 改了什么

| 维度 | 改动前 | 改动后 |
|------|--------|--------|
| 调用方式 | 前端 `api('/api/profiles')` → WebUI 本地 | 前端 `apiExternal()` → 外部服务 |
| 响应字段 | `name/path/model/provider/skill_count/...` | 同上 + **`display_name`**、**`description`**、**`logo_base64`** |
| logo 来源 | 无 | 读各 profile 目录下 `info.json` 的 `logo` 字段（本地绝对路径），转 Data URI |
| 缓存 | 无 | logo Base64 TTL 缓存 5 分钟，单图上限 100KB |
| CSP | 无额外 connect-src | `api/helpers.py` 动态追加外部服务 URL 到 `connect-src` |

#### 外部服务实现逻辑

```python
# hermes-external-service/routers/profiles.py
for base_info in get_all_profiles():
    extra = _read_info_json(base_info["path"])  # display_name, description, logo_base64
    base_info.update(extra)
return {"profiles": profiles, "active": active}
```

`info.json` 示例：

```json
{
  "display_name": "代码助手",
  "description": "用于写代码的助手",
  "logo": "/data/.hermes/logos/logo2.png"
}
```

缺失或异常时：`display_name`/`description` 为空字符串，`logo_base64` 为 `null`。

#### 前端渲染变化

`panels.js` 中 profile 卡片/下拉菜单：

- 主标题用 `p.display_name || p.name`
- 副标题显示 `p.name`（当 display_name 与 name 不同时）
- `<img src="${p.logo_base64}">` 展示图标

#### 注意

- Profile **切换/创建/删除** 仍走 WebUI 本地 `POST /api/profile/switch` 等，不经过外部服务。
- 未配置 `HERMES_EXTERNAL_SERVICE_PUBLIC` 时，`apiExternal()` 抛 `ExternalServiceNotConfigured`，前端降级为空列表或 toast 提示。

---

### 3.2 GET `/api/skills` — 本地 Skills 列表

**调用路径：前端 → WebUI 后端代理 → 外部服务**

| 层级 | 文件 |
|------|------|
| 前端 | `panels.js` → `loadSkills()`、`commands.js` |
| WebUI 代理 | `routes.py` → `_ext_service_proxy_skills_list()` |
| 外部服务 | `routers/local_skills.py` → `list_local_skills()` |

#### 请求参数

| 参数 | 来源 | 说明 |
|------|------|------|
| `profile` | query 或 cookie，默认 `DEFAULT` | 决定读取哪个 profile 的 skills 目录 |
| `category` | query（可选） | WebUI 代理透传（外部服务当前未使用此参数过滤） |

#### 调用流程

```
panels.js: api('/api/skills?profile=xxx')
  └─ WebUI GET /api/skills
       └─ if HERMES_EXTERNAL_SERVICE:
            _ext_service_request('/api/skills', params={profile, category?})
              └─ external-service GET /api/skills?profile=xxx
                   ├─ get_local_skills_dir(profile)
                   ├─ get_local_skills(profile)          递归扫描
                   └─ _parse_skill_metadata()             解析 SKILL.md frontmatter
```

#### 改了什么

| 维度 | 改动前 | 改动后（走外部服务） |
|------|--------|---------------------|
| 数据来源 | WebUI 进程内 `_skills_list_from_dir(_active_skills_dir())` | 外部服务读 `HERMES_RUNTIME_DIR` 对应 profile 目录 |
| profile 隔离 | 仅当前 WebUI active profile 的 skills | 显式 `?profile=` 参数，支持跨 profile 查看 |
| 响应字段 | `name/description/category/hub_installed/can_delete` | 新增 **`full_name`**（含嵌套路径）、**`version`**、**`author`**、**`install_name`** |
| 嵌套 skill | 不支持 | 支持 `category/sub-skill` 递归目录结构 |
| hub_installed 判断 | 查 `.hub/lock.json` | 查目录下是否存在 `.hub_installed` 文件 |
| can_delete 判断 | 是否在 lock.json 中 | 是否系统 skill（`is_system_skill()`） |

#### 外部服务返回示例

```json
{
  "skills": [
    {
      "name": "agent-browser",
      "category": "tools",
      "full_name": "tools/agent-browser",
      "description": "浏览器自动化",
      "version": "1.0",
      "author": "hermes",
      "hub_installed": true,
      "can_delete": true,
      "install_name": "Agent Browser"
    }
  ]
}
```

#### 前端变化

- 新增 `_skillsProfileContext` 状态，skills 面板和 profile 详情页分别绑定不同 profile
- Cron/Commands 固定传 `?profile=DEFAULT`
- `renderSkills()` 过滤 `s.group` 非空项，只展示顶层 skill

#### Fallback（未配置外部服务）

WebUI 仍走 `_skills_list_from_dir()` + lock.json 标注 `hub_installed`。

---

### 3.3 GET `/api/skills/content` — 读取 Skill 内容

**调用路径：前端 → WebUI 后端代理 → 外部服务**

| 层级 | 文件 |
|------|------|
| 前端 | `panels.js` → `openSkill()`、`openSkillFile()` |
| WebUI 代理 | `routes.py` → `_ext_service_proxy_skill_content()` |
| 外部服务 | `routers/local_skills.py` → `get_local_skill_content()` |

#### 请求参数

| 参数 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | skill 名称（支持嵌套路径如 `tools/agent-browser`） |
| `file` | 否 | 附属文件相对路径；为空时返回主文档 |
| `profile` | 否 | 默认 `DEFAULT` |

#### 调用流程

```
panels.js: api('/api/skills/content?name=x&profile=y&file=z')
  └─ WebUI GET /api/skills/content
       └─ _ext_service_request('/api/skills/content', params={name, profile, file?})
            └─ external-service
                 ├─ file 为空 → find_skill_main_file() → 读 SKILL.md
                 └─ file 有值 → 读 skill_path/file
                 返回 PlainTextResponse（纯 markdown 文本）
       └─ WebUI 包装：若响应为 str → {"content": text, "linked_files": {}}
```

#### 改了什么

| 维度 | 改动前 | 改动后 |
|------|--------|--------|
| 响应格式 | JSON `{content, linked_files}` | 外部服务返回 **纯文本**；WebUI 代理层包装为 JSON |
| linked_files | WebUI 本地扫描 skill 目录附属文件 | 外部服务不返回 linked_files，代理层固定 `{}` |
| profile | 仅 active profile | 支持 `?profile=` 跨 profile 读取 |

#### Fallback

WebUI 本地 `_skill_view_from_active_dir()` 或读指定 file，返回完整 linked_files。

---

### 3.4 POST `/api/skills/save` — 保存/更新 Skill

**调用路径：前端 → WebUI 后端代理 → 外部服务**

| 层级 | 文件 |
|------|------|
| 前端 | `panels.js` → `saveSkillForm()` |
| WebUI 代理 | `routes.py` → `_ext_service_proxy_skill_save()` |
| 外部服务 | `routers/local_skills.py` → `save_local_skill()` |

#### 请求体

```json
{
  "name": "my-skill",
  "content": "# My Skill\n...",
  "category": "tools",
  "profile": "abc"
}
```

WebUI 代理将 `profile` 从 body 或 cookie 解析后作为 **query 参数** 转发：`POST /api/skills/save?profile=abc`。

#### 外部服务逻辑

1. `skills_dir = get_local_skills_dir(profile)`
2. 创建 `{skills_dir}/{name}/` 目录
3. 写入 `SKILL.md`
4. 若有 category，写入 `.category` 文件

#### 改了什么

| 维度 | 改动前 | 改动后 |
|------|--------|--------|
| 写入位置 | WebUI active profile 的 `HERMES_HOME/skills` | 外部服务 `{HERMES_RUNTIME_DIR}/profiles/{profile}/skills` |
| profile 参数 | 无 | body/query 均支持 |
| 返回 | WebUI 本地格式 | `{ok, name, path}` |

#### Fallback

WebUI `_handle_skill_save()` 本地写入。

---

### 3.5 POST `/api/skills/delete` — 删除 Skill

**调用路径：前端 → WebUI 后端代理 → 外部服务**

| 层级 | 文件 |
|------|------|
| 前端 | `panels.js` → `deleteSkill()` |
| WebUI 代理 | `routes.py` → `_ext_service_proxy_skill_delete()` |
| 外部服务 | `routers/local_skills.py` → `delete_local_skill()` |

#### 请求体

```json
{"name": "my-skill", "profile": "abc"}
```

#### 外部服务逻辑

1. 定位 `{skills_dir}/{name}`
2. 若 `is_system_skill()` → 403 拒绝
3. `shutil.rmtree()` 删除

#### 改了什么

| 维度 | 改动前 | 改动后 |
|------|--------|--------|
| 删除范围 | WebUI active profile skills | 指定 profile 的 skills 目录 |
| 系统 skill 保护 | WebUI 本地逻辑 | 外部服务 `is_system_skill()` 检查 |
| lock.json 同步 | WebUI fallback 会更新 `.hub/lock.json` | 外部服务**不操作** lock.json |

---

### 3.6 GET `/api/skillhub/skills` — SkillHub 远端列表

**调用路径：前端 → WebUI 后端代理 → 外部服务 → SkillHub 上游**

| 层级 | 文件 |
|------|------|
| 前端 | `panels.js` → `loadSkillHub()` |
| WebUI 代理 | `routes.py` → `_ext_service_proxy_skillhub_skills()` |
| 外部服务 | `routers/skillhub.py` → `list_skillhub_skills()` |
| 上游 | `GET {SKILLHUB_URL}/api/skills` |

#### 请求参数

| 参数 | 说明 |
|------|------|
| `profile` | 用于对比本地安装状态 |
| `q` | 搜索关键字 |
| `category` | 分类筛选 |
| `page` / `page_size` | 分页 |

#### 调用流程

```
panels.js: api('/api/skillhub/skills?profile=xxx')
  └─ WebUI GET /api/skillhub/skills
       └─ _ext_service_request('/api/skillhub/skills', params={profile, q?, category?, page?, page_size?})
            └─ external-service
                 ├─ httpx GET {SKILLHUB_URL}/api/skills
                 ├─ get_local_skills(profile) → local_names
                 └─ 为每个远端 skill 标注 status="已安装"/"未安装", installed=bool
       └─ WebUI 额外映射：status → installed boolean（兼容前端）
       └─ 返回 {"enabled": true, "skills": [...]}
```

#### 改了什么

| 维度 | 改动前（WebUI fallback） | 改动后（外部服务） |
|------|------------------------|-------------------|
| 安装状态判断 | 对比 lock.json + 本地目录名 | 对比 `get_local_skills(profile)` 集合 |
| 状态字段 | 直接返回 `installed: bool` | 外部服务返回 `status: "已安装"/"未安装"`，WebUI 映射为 `installed` |
| 分页 | 不支持 | 支持 `page/page_size`，透传 `total` |
| profile | 仅 active profile | 显式 `?profile=` |
| 上游路径 | `GET /api/skills` | 同左（一致） |

#### WebUI 映射逻辑

```python
# routes.py L4726-4729
for skill in skills:
    status = str(skill.get("status") or "").strip()
    if status:
        skill["installed"] = status in ("已安装", "installed", "true", "yes", "1")
```

---

### 3.7 GET `/api/skillhub/categories` — SkillHub 类目

**调用路径：WebUI 后端代理 → 外部服务 → SkillHub 上游**

| 层级 | 文件 |
|------|------|
| WebUI 代理 | `routes.py` → `_ext_service_proxy_skillhub_categories()` |
| 外部服务 | `routers/skillhub.py` → `list_skillhub_categories()` |
| 上游 | `GET {SKILLHUB_URL}/api/skills/categories` |

#### 改了什么

| 维度 | 改动前 fallback | 改动后 |
|------|----------------|--------|
| 上游路径 | `GET /api/categories` | `GET /api/skills/categories` **路径不同** |
| 前端 | 当前未调用此接口 | — |

---

### 3.8 GET `/api/skillhub/content` — SkillHub 文档预览

**调用路径：前端 → WebUI 后端代理 → 外部服务 → SkillHub 上游**

| 层级 | 文件 |
|------|------|
| 前端 | `panels.js` → `viewSkillHubContent()` |
| WebUI 代理 | `routes.py` → `_ext_service_proxy_skillhub_content()` |
| 外部服务 | `routers/skillhub.py` → `get_skillhub_content()` |
| 上游 | `GET {SKILLHUB_URL}/api/skills/{name}/doc` |

#### 改了什么

| 维度 | 改动前 fallback | 改动后 |
|------|----------------|--------|
| 上游路径 | `GET /api/skills/{name}` | `GET /api/skills/{name}/doc` **路径不同** |
| 前端读取 | `data.content \|\| data.readme` | 同左（兼容） |

---

### 3.9 GET `/api/skillhub/file` — SkillHub 附属文件

**调用路径：WebUI 后端代理 → 外部服务 → SkillHub 上游**

| 层级 | 文件 |
|------|------|
| WebUI 代理 | `routes.py` → `_ext_service_proxy_skillhub_file()` |
| 外部服务 | `routers/skillhub.py` → `get_skillhub_file()` |
| 上游 | `GET {SKILLHUB_URL}/api/skills/{name}/file?path=...` |

#### 参数映射

WebUI 接受 `file` 或 `path` query 参数，转发给外部服务时统一为 `path`。

#### 改了什么

- 外部服务返回 `PlainTextResponse`（纯文本），WebUI 直接 JSON 包装返回
- 前端当前**未调用**此接口

---

### 3.10 POST `/api/skillhub/install` — 从 SkillHub 安装

**调用路径：前端 → WebUI 后端代理 → 外部服务 → SkillHub 上游 + 本地落盘**

| 层级 | 文件 |
|------|------|
| 前端 | `panels.js` → `installSkillHub()` |
| WebUI 代理 | `routes.py` → `_ext_service_proxy_skillhub_install()` |
| 外部服务 | `routers/skillhub.py` → `install_skillhub_skill()` |

#### 请求体

```json
{
  "name": "data-analysis",
  "display_name": "Data Analysis",
  "profile": "abc"
}
```

WebUI 代理从 body 提取 `name/display_name/profile`，组装后 POST 给外部服务。

#### 外部服务安装流程

```
1. 检查 target_path 是否已安装（SKILL.md / .hub_installed / {name}.md）
   └─ 已安装 → 409

2. 尝试 ZIP 安装
   GET {SKILLHUB_URL}/api/skills/{name}/download
   └─ 200 → extract_zip_and_flatten → 写 .hub_installed + .install_name

3. ZIP 失败 → doc fallback
   GET {SKILLHUB_URL}/api/skills/{name}/doc
   └─ 写 SKILL.md → 写 .hub_installed + .install_name

4. 落盘路径
   profile=DEFAULT → {HERMES_RUNTIME_DIR}/skills/{name}/
   profile=abc     → {HERMES_RUNTIME_DIR}/profiles/abc/skills/{name}/
```

#### 改了什么

| 维度 | 改动前（WebUI fallback） | 改动后（外部服务） |
|------|------------------------|-------------------|
| 安装方式 | fetch bundle → **quarantine → scan_skill → install_from_quarantine** | ZIP 下载 / doc fallback，**无安全扫描** |
| 落盘位置 | WebUI active profile `HERMES_HOME/skills` | 外部服务 `HERMES_RUNTIME_DIR` 按 profile 隔离 |
| lock.json | 写入 `.hub/lock.json` | **不写入** lock.json，仅写 `.hub_installed` |
| profile | 无 | body 带 `profile` |
| 返回 | `{ok, name, dir}` | WebUI 代理简化为 `{ok: true, name}` |

#### 前端联动

安装成功后 `loadSkillHub(true, pr)` + `loadSkills(pr)` 双刷新。

---

### 3.11 POST `/api/skillhub/uninstall` — 卸载 Skill

**调用路径：前端 → WebUI 后端代理 → 外部服务**

| 层级 | 文件 |
|------|------|
| 前端 | `panels.js` → `uninstallSkillHub()` |
| WebUI 代理 | `routes.py` → `_ext_service_proxy_skillhub_uninstall()` |
| 外部服务 | `routers/skillhub.py` → `uninstall_skillhub_skill()` |

#### 请求体

```json
{
  "name": "data-analysis",
  "display_name": "Data Analysis",
  "install_name": "data-analysis",
  "profile": "abc"
}
```

#### 外部服务逻辑

1. `target_name = install_name or name`
2. `shutil.rmtree({skills_dir}/{target_name})`
3. 不存在 → 404

#### 改了什么

| 维度 | 改动前 fallback | 改动后 |
|------|----------------|--------|
| 删除方式 | 删目录 + 更新 lock.json | 仅删目录 |
| profile | active profile | 指定 profile |
| 查找 skill | `_find_skill_in_dirs()` 多目录搜索 | 固定 profile skills 目录 |

---

### 3.12 GET `/api/skillhub/structure` — 未接通

| 状态 | 说明 |
|------|------|
| 外部服务 | 已实现：`routers/skillhub.py` → `get_skillhub_structure()` |
| WebUI 代理函数 | 已实现：`_ext_service_proxy_skillhub_structure()` |
| WebUI 路由 | **未注册** `GET /api/skillhub/structure` |
| 前端 | **未调用** |

---

## 4. 前端调用点汇总

### 4.1 apiExternal() — 直连外部服务

文件：`static/workspace.js`

| 调用位置 | 函数 | 用途 |
|---------|------|------|
| `panels.js:361` | `loadCronProfiles()` | Cron 表单 profile 下拉选项 |
| `panels.js:1916` | `_kanbanLoadProfileNames()` | Kanban 任务 assignee 选项 |
| `panels.js:4525` | `loadProfilesPanel()` | Profiles 管理面板 |
| `panels.js:4857` | `toggleProfileDropdown()` | 顶部 profile 切换下拉 |

### 4.2 api() — 经 WebUI 后端（可能代理到外部服务）

| 调用位置 | 接口 | profile 传参 |
|---------|------|-------------|
| `panels.js:3251` | `GET /api/skills` | `_skillsProfileContext` |
| `panels.js:3412/3427` | `GET /api/skills/content` | 同上 |
| `panels.js:3556` | `POST /api/skills/save` | body.profile |
| `panels.js:3595` | `POST /api/skills/delete` | body.profile |
| `panels.js:4730/4768` | skills（profile 详情页） | profileName |
| `panels.js:7091` | `GET /api/skillhub/skills` | `_skillsProfileContext` |
| `panels.js:7154` | `POST /api/skillhub/install` | body.profile |
| `panels.js:7169` | `POST /api/skillhub/uninstall` | body.profile |
| `panels.js:7183` | `GET /api/skillhub/content` | 无 |
| `panels.js:765/797` | `GET /api/skills?profile=DEFAULT` | Cron |
| `commands.js:712/1292` | `GET /api/skills?profile=DEFAULT` | 命令补全 |

---

## 5. external-service 目录结构

```
hermes-external-service/
├── main.py                    # FastAPI 入口，注册路由 + CORS
├── config.py                  # HERMES_RUNTIME_DIR / SKILLHUB_URL / profile 路径
├── models.py                  # SaveSkillRequest / InstallSkillRequest 等
├── utils.py                   # find_skill_main_file / extract_zip_and_flatten
├── routers/
│   ├── local_skills.py        # /api/skills  CRUD（4 个接口）
│   ├── skillhub.py            # /api/skillhub/* 代理 + 安装/卸载（7 个接口）
│   └── profiles.py            # /api/profiles  含 logo Base64（1 个接口）
├── apidocs.md                 # 接口需求文档
├── api_profiles_方案.md        # profiles 扩展方案
└── tests/                     # pytest 覆盖全部路由
```

---

## 6. WebUI 侧改动文件清单

| 文件 | 改动内容 |
|------|---------|
| `api/routes.py` | 新增 `_ext_service_*` 代理 helpers；Skills/SkillHub 路由加优先代理分支 |
| `api/helpers.py` | CSP `connect-src` 动态追加外部服务 URL |
| `api/profiles.py` | hermes_cli 不可用时 fallback 扫描 profiles 目录 |
| `static/workspace.js` | 新增 `apiExternal()` 直连 helper |
| `static/index.html` | 注入 `window.__HERMES_CONFIG__.externalServiceUrl` |
| `static/panels.js` | profiles 改 apiExternal；skills/skillhub 加 profile 参数；渲染 logo |
| `static/style.css` | profile logo 尺寸样式 |
| `static/commands.js` | skills 请求加 `?profile=DEFAULT` |
| `.env.example` | 新增 `HERMES_EXTERNAL_SERVICE` 说明 |
| `.env.docker.example` | 同上 |
| `hermes-extrenal-service-openapi.json` | 外部服务 OpenAPI 快照 |

---

## 7. 已知差异与缺口

| 项 | 说明 |
|----|------|
| Profiles 双源 | 展示走外部服务，切换/创建走 WebUI 本地，字段不完全一致 |
| structure 未接通 | 代理函数已实现但 WebUI 无路由、前端无调用 |
| 安装安全策略 | 外部服务无 quarantine/scan，WebUI fallback 有 |
| lock.json | 外部服务用 `.hub_installed` 标记，不维护 lock.json |
| categories 上游路径 | fallback 用 `/api/categories`，外部服务用 `/api/skills/categories` |
| 单容器部署 | `start-all.sh` 启动外部服务，但 compose 未默认设置 `HERMES_EXTERNAL_SERVICE` |

---

## 8. 配置示例

### 本地开发

```bash
# WebUI
export HERMES_EXTERNAL_SERVICE=http://127.0.0.1:18788
export HERMES_EXTERNAL_SERVICE_PUBLIC=http://127.0.0.1:18788

# external-service
export HERMES_RUNTIME_DIR=~/.hermes
export SKILLHUB_URL=https://your-skillhub.example.com
export SERVICE_PORT=18788
```

### Docker 单容器（需手动补充）

```bash
# WebUI 容器内访问 external（同容器 localhost）
HERMES_EXTERNAL_SERVICE=http://127.0.0.1:18788
# 浏览器访问 external（宿主机映射端口）
HERMES_EXTERNAL_SERVICE_PUBLIC=http://127.0.0.1:18788
```
