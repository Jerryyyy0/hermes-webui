# 接口文档
以下内容来源于项目内的Markdown文档文件，可直接维护并同步展示。该文档作为对外暴露接口的约束文档，里面涉及到的接口必须严格按照接口文档的要求开发。

## 基础信息

●基础地址: http://localhost:8000
●接口前缀: /api/skills、/api/admin、/api/external
●数据格式:除下载接口外，默认返回 application/json
●字符编码: UTF-8

## 状态码约定

200:请求成功
400:请求参数错误或上传文件不符合要求
404:资源不存在
500:服务端处理失败
502:上游大模型不可用（仅「重新翻译提取并保存」接口，表示未落库、可重试）

## 技能接口

### 1.获取分类列表✅

●方法: GET
●路径: /api/skills/categories
●说明: 返回当前系统中 **category 维度**下的所有唯一取值名。为兼容旧调用方保留；等价于「获取维度列表」中 `category` 维度的取值名子集。多维度请改用 `/api/skills/dimensions`。

响应示例

```
[
"audit",
"data-analysis"
]
```

### 1b.获取维度列表（多维度）

●方法: GET
●路径: /api/skills/dimensions
●说明: 返回当前系统全部**启用维度**（含手动维护与自动派生，如年份）及其**启用取值**，供按 `?dimension=维度码:取值名` 组合筛选技能。仅暴露对外筛选所需字段。

响应字段说明（数组项）

| 字段        | 类型     | 说明                                                     |
| ----------- | -------- | -------------------------------------------------------- |
| code        | string   | 维度机读码（如 `category`、`year`），用于 `dimension` 筛选 |
| name        | string   | 维度显示名                                               |
| multi_value | boolean  | 该维度是否允许技能挂多个取值                             |
| values      | object[] | 该维度下的启用取值，元素含 `code`（取值机读码）与 `name`（取值显示名） |

响应示例

```
[
    {
        "code": "category",
        "name": "分类",
        "multi_value": false,
        "values": [
            { "code": "web", "name": "网页开发" },
            { "code": "data-analysis", "name": "数据分析" }
        ]
    },
    {
        "code": "year",
        "name": "年份",
        "multi_value": false,
        "values": [
            { "code": "2024", "name": "2024" },
            { "code": "2025", "name": "2025" }
        ]
    }
]
```

### 2.获取技能列表✅

●方法: GET
●路径: /api/skills
●说明: 分页查询技能列表，支持关键字、分类与**多维度**筛选

查询参数

| 参数名    | 类型    | 必填 | 默认值 | 说明                 |
| --------- | ------- | ---- | ------ | -------------------- |
| q         | string  | 否   | /      | 搜索关键字           |
| category  | string  | 否   | /      | 分类筛选（等价于 `dimension=category:取值名`；与 `dimension` 同时传时取交集） |
| dimension | string  | 否   | /      | 多维度筛选，格式 `维度码:取值名`（**按取值名匹配**）。**可重复**传多个，多个维度取**交集**（AND）。维度码/取值名取自「获取维度列表」。非法项（无冒号、码或值为空）忽略 |
| page      | integer | 否   | 1      | 页码，从1开始        |
| page_size | integer | 否   | 20     | 每页数量，范围 **1-50**（`<1` 回落默认 20，`>50` 收敛为 50） |

请求示例

```
GET  /api/skills?q=data&page=1&page_size=9

# 多维度筛选：category 维度取值名为「数据分析」且 year 维度取值名为「2024」
GET  /api/skills?dimension=category:数据分析&dimension=year:2024
```

> 说明：本列表接口的响应结构基本不变（仍含 `category` 单值等字段），`dimension` 仅作为筛选条件；技能的**完整多维度信息**见「获取单个技能信息」的 `dimensions` 字段。

响应顶层字段

| 字段       | 类型     | 说明                                                     |
| ---------- | -------- | -------------------------------------------------------- |
| skills     | object[] | 本页技能列表                                             |
| name_list  | string[] | **本页 `skills` 各项 `name` 的合集**（顺序与 `skills` 一致，便于直接取名，如批量下载编排） |
| total      | integer  | 符合条件的技能总数（跨页）                               |
| page       | integer  | 当前页码                                                 |
| page_size  | integer  | 生效的每页数量（已按 1-50 钳制后的值）                   |

响应示例

```
{
    "skills":[
    {
    	"name":"data-analysis",
    	"display_name":"data-analysis",
    	"description":"Comprehensive CSV data analysis tool",
    	"category":"data-analysis",
    	"tags":[
    		"csv",
    		"analysis"
    	],
    	"download_count": 4
    }
    ],
    "name_list":[
        "data-analysis"
    ],
    "total": 1,
    "page": 1,
    "page_size": 9
}
```

### 3.获取单个技能信息✅

●方法: GET
●路径: /api/skills/{name}
●说明:根据技能名称获取技能详情

路径参数

| 参数名 | 类型   | 必填 | 说明     |
| ------ | ------ | ---- | -------- |
| name   | string | 是   | 技能名称 |

响应字段说明

| 字段                | 类型           | 说明                                                         |
| ------------------- | -------------- | ------------------------------------------------------------ |
| name                | string         | 技能名称（唯一标识）                                         |
| display_name        | string         | 技能显示名称                                                 |
| description         | string         | 技能描述（英文）                                             |
| display_description | string         | 展示用中文描述                                               |
| category            | string         | 分类名称（`dimensions` 中 `category` 维度的第一个取值名，兼容保留） |
| dimensions          | object         | 多维度信息，键为维度码（如 `category`、`year`），值为该维度取值名数组；含手动 + 自动派生维度 |
| tags                | string[]       | 技能标签                                                     |
| download_count      | integer        | 累计安装数（对外字段名仍为 download_count）                  |
| detail_json         | object \| null | AI 提取的技能详情，已解析为嵌套 JSON 对象；缺失或不可解析时为 null |
| published_at        | string \| null | 发布时间，格式 `yyyy-MM-dd HH:mm:ss`（取审核通过时间）       |
| favorite_count      | integer        | 点赞数                                                       |

响应示例

```
{
    "name":"data-analysis",
    "display_name":"data-analysis",
    "description":"Comprehensive CSV data analysis tool",
    "display_description":"全面的 CSV 数据分析工具",
    "category":"data-analysis",
    "dimensions":{
        "category":["data-analysis"],
        "year":["2024"]
    },
    "tags":[
        "csv",
        "analysis"
    ],
    "download_count": 4,
    "detail_json":{
        "taskGoal":"对用户提供的 CSV 数据进行统计分析并产出结论",
        "taskDetails":[
            "解析并校验 CSV 结构",
            "计算描述性统计指标"
        ],
        "useMode":"对话中发送包含\"分析\"、\"统计\"等语义的 CSV 处理请求即可触发",
        "triggerKeywords":[
            "数据分析",
            "CSV 统计"
        ],
        "requiredInfo":[
            {
                "label":"待分析的 CSV 文件（必填）",
                "required":true
            }
        ],
        "dialogExample":{
            "user":"帮我分析这份销售数据的月度趋势",
            "assistant":"好的，正在解析 CSV 并计算月度趋势..."
        }
    },
    "published_at":"2026-06-25 17:30:00",
    "favorite_count": 12
}
```

### 4.下载技能包✅

●方法: GET
●路径: /api/skills/{name}/download
●说明: 下载技能ZIP包，成功时返回二进制文件流，并自动累计下载次数
路径参数

| 参数名 | 类型   | 必填 | 说明     |
| ------ | ------ | ---- | -------- |
| name   | string | 是   | 技能名称 |

响应说明
●成功时返回 application/zip
●文件名格式:{name}.zip

### 5.批量下载技能包

●方法: POST
●路径: /api/skills/batch/download
●说明: 一次性下载多个技能，返回外层 ZIP（`skill_batch.zip`），内含 N 个 `<name>.zip`。**每个内层 zip 与单技能下载（接口 4）产物逐字节一致**，与批量上传（管理接口 2 的 batch 模式）为逆对称格式。此接口为纯读，**不累计安装数**。
●请求类型: application/json

请求体参数

| 参数名 | 类型     | 必填 | 说明                                                         |
| ------ | -------- | ---- | ------------------------------------------------------------ |
| names  | string[] | 否   | 要下载的技能名列表。传入非空数组时只打包指定技能；为空数组或整个请求体为空 `{}` 时打包**全部已上架技能** |

行为约束

●选择口径与单技能下载/技能列表一致：仅 `status=已上架` 且未隐藏、未彻底删除的技能。
●传入的 `names` 中查不到、或该技能无文件的项**直接跳过**，不计入外层 zip，也不报错。
●全部未命中（指定的 name 都不存在，或全量时市场为空）返回 **404**。
●单次命中技能数上限 **50**；超出返回 **400**，需改用 `names` 指定或分批下载。
●外层 zip 内的内层包命名为 `<name>.zip`；同名技能已按 name 去重，不会重复。

响应说明
●成功时返回 application/zip
●文件名格式: `skill_batch.zip`
●内层结构:

```
skill_batch.zip
├── data-analysis.zip     ← 等同 GET /api/skills/data-analysis/download 的产物
├── contract-audit.zip
└── ...
```

请求示例

```
POST /api/skills/batch/download
Content-Type: application/json

{ "names": ["data-analysis", "contract-audit"] }
```

```
POST /api/skills/batch/download
Content-Type: application/json

{}          # 打包全部已上架技能
```

### 6.获取技能文档✅

●方法: GET
●路径:/api/skills/{name}/doc
●说明:读取技能目录中的SKILL.md文档内容

路径参数

| 参数名 | 类型   | 必填 | 说明     |
| ------ | ------ | ---- | -------- |
| name   | string | 是   | 技能名称 |

响应示例

```
{
	"name":"data-analysis",
	"content":"#技能说明\\n\\\n这里是技能文档内容"
}
```

### 7.获取技能目录结构✅

●方法: GET
●路径: /api/skills/{name}/structure
●说明: 返回技能全部文件的扁平路径列表（不再按顶层目录分类）：
  - `name`：技能名称
  - `pathList`：技能文件路径键值对数组，每项含 `name`（文件名）、`path`（文件路径）
  - 目录占位项（is_directory=1）不返回

路径参数

| 参数名 | 类型   | 必填 | 说明     |
| ------ | ------ | ---- | -------- |
| name   | string | 是   | 技能名称 |

响应示例

```
{
    "name":"data-analysis",
    "pathList":[
        {
            "name":"SKILL.md",
            "path":"SKILL.md"
        },
        {
            "name":"LICENSE.txt",
            "path":"LICENSE.txt"
        },
        {
            "name":"run.py",
            "path":"scripts/run.py"
        },
        {
            "name":"README.md",
            "path":"references/README.md"
        }
    ]
}
```

### 8.获取技能文件内容✅

●方法: GET
●路径: /api/skills/{name}/file
●说明: 读取技能目录下指定相对路径文件的内容

路径参数

| 参数名 | 类型   | 必填 | 说明     |
| ------ | ------ | ---- | -------- |
| name   | string | 是   | 技能名称 |

查询参数

| 参数名 | 类型   | 必填 | 说明                     |
| ------ | ------ | ---- | ------------------------ |
| path   | string | 是   | 技能目录内的相对文件路径 |

请求示例

```
GET /api/skills/data-analysis/file?path=README.md
```

响应示例

```
{
	"name":"data-analysis",
    "path":"README.md",
	"content":"# README\\n\\\n文件内容"
}
```

### 9.删除技能

●方法: DELETE
●路径: /api/skills/{name}
●说明: **完整拆除**指定技能——业务上等价于「从技能市场下架 + 从『我的技能-来自用户』删除 + 从『我的管理』彻底删除」三步组合：下架（若在架）→ 释放创建者 → 彻底删除（移出市场、禁止新安装、首轮回收无引用快照；创建者已释放且无人安装时级联全清该技能全部数据，name 随之释放可再上传；仍有人安装则残留供其读取，最后一个卸载时回收）。
●幂等：已彻底删除的技能再次 DELETE 返回 200 成功；该 name 从未存在返回 404。
●无鉴权（与其它对外接口一致），任意调用方均可触发。

路径参数

| 参数名 | 类型   | 必填 | 说明     |
| ------ | ------ | ---- | -------- |
| name   | string | 是   | 技能名称 |

响应示例

```
{
	"message":"Skill deleted successfully",
	"name":"data-analysis"
}
```

## 管理接口

### 1.刷新技能索引✅

●方法: POST
●路径: /api/admin/refresh
●说明: 重新扫描技能目录，并同步到数据库

响应示例

```
{
	"message":"Skills synced successfully"
}
```

### 2.上传技能包

●方法: POST
●路径: /api/admin/upload
●说明: 上传ZIP文件并自动解压到技能目录，要求压缩包内包含SKILL.md。上传后会**自动调用大模型补全技能元信息**（与页面上传同一套能力）并**自动发起上架审核**；`auto_approve` 默认 `true`，会紧接着**自动审核通过**使技能直接上架（`auto_approve=false` 时仅停在待上架审核）。支持 `batch=true` 一次上传外层 ZIP（内含 N 个技能 ZIP）批量创建。
●请求类型: multipart/form-data

表单参数

| 参数名  | 类型    | 必填 | 说明                                                         |
| ------- | ------- | ---- | ------------------------------------------------------------ |
| file    | file    | 是   | `batch=false` 时为单个技能ZIP文件；`batch=true` 时为外层ZIP（内含 N 个技能 ZIP）。最大500MB |
| user_id | integer | 否   | 归属用户ID；为空默认归属 admin，非空归属对应用户（用户不存在则拒绝）。审核人始终为 admin |
| platform | string | 否 | 第三方平台标识。与 `external_user_id` **成对出现**才把该技能标记为第三方外部上传（记录外部身份，供后续通过接口「外部审核结果接口」轮询审核结果）；单独出现或都不传时忽略，按站内上传处理。仅单个上传（`batch=false`）生效 |
| external_user_id | string | 否 | 第三方平台侧的用户唯一标识。须与 `platform` 成对出现；用于标识"这条上传属于第三方哪个用户"，SkillHub 不会为其创建系统账号 |
| batch   | boolean | 否   | 是否批量上传，默认 `false`。为 `true` 时解析外层 ZIP 内每个 `.zip` 为一个独立技能 |
| auto_approve | boolean | 否 | 是否自动审核通过，默认 `true`。`true` 时上传后自动完成上架审核（技能直接上架 status=2）；`false` 时仅提交上架审核、停在待审核（status=1）。单个与批量均适用 |

约束说明

●文件名扩展名必须为.zip
●技能名（标识 slug）优先取 SKILL.md 的 `name` 字段，提取不到时用文件名（去扩展名）兜底；非法字符替换为连字符
●技能名只允许字母、数字、连字符和下划线，且全局唯一
●压缩包内必须包含 SKILL.md
●接口上传后会**自动发起上架审核**（审核人固定为 admin）；此路径无人工选型环节，**技能类型默认通用型（skillType=0）、无归属部门（dept_id=null），即全员可见**。技能类型在「首次上架」步骤确定，与页面上传保持同一模型。

AI 元信息补全说明

●上传时会以 SKILL.md 内容调用大模型，自动补全以下入库字段（与页面上传一致）：
  - **显示名称（display_name）**：大模型翻译的中文显示名
  - **描述（description）**：大模型提取/整理的英文描述
  - **展示描述（display_description）**：大模型翻译的中文展示描述
  - **技能详情（detail_json）**：大模型从 SKILL.md 结构化提取的详情 JSON（任务目标、触发关键词、所需信息、对话示例等）
●**技能标识 slug（name）不受大模型影响**，仍按上方规则取自 SKILL.md/文件名，以保证对外标识、下载地址与唯一性稳定。
●大模型能力为**容错降级**：未启用或调用失败时，上述字段回退到 SKILL.md 解析值（detail_json 为空），**上传与审核流程不受影响**。

第三方外部上传说明（platform + external_user_id）

●面向"第三方平台代其用户上传技能"的场景：第三方用户不在 SkillHub 的用户体系内，无需也不会为其创建系统账号。
●上传时携带 `platform` 与 `external_user_id`（**必须成对**），SkillHub 会把这对外部身份记录在该技能上；技能归属仍按 `user_id` 规则（为空则 admin），审核人恒为 admin。
●只传其一（仅 `platform` 或仅 `external_user_id`）视为无效外部标识，**忽略**、按普通站内上传处理，**不报错**（向后兼容）。
●上架/下架审核结束（通过或驳回）后，会为带外部身份的技能生成一条审核结果事件；第三方通过「外部审核结果接口」（`GET /api/external/notifications`）按 `platform + external_user_id` 轮询获取。
●批量上传（`batch=true`）不支持外部身份，`platform`/`external_user_id` 不生效。

成功响应示例（`auto_approve=true`，默认）

```
{
    "message":"Skill uploaded and listed",
    "name":"data-analysis",
    "skillName":"数据分析",
    "skillId":"a1b2-c3d4-e5f6-7890",
    "status":"2"
}
```

> `auto_approve=false` 时仅提交审核，返回 `"status":"1"`、`"message":"Skill uploaded and submitted for listing audit"`；若自动通过瞬间市场已存在同名在架技能，则通过被降级为驳回，返回 `"status":"0"`。

失败响应示例

```
{
	"detail":"Only ZIP files are allowed"
}
```

批量上传说明（batch=true）

●外层 ZIP 的**根层级**须平铺 N 个技能 `.zip`（与批量下载的 `skill_batch.zip` 逆对称）；每个内层 `.zip` 即一个独立技能包，规则同单个上传。
●单次最多 **20** 个技能，超出返回 **400**。
●外层包内未找到任何 `.zip` 返回 **400**。
●**逐项独立处理**：每个内层技能各自解析 → AI 补全 → 创建 → 文件入库 → 发起上架 →（`auto_approve=true` 时）自动通过（独立事务），单项失败不影响其它项，结果在 `results` 中逐项返回。

批量上传外层结构

```
outer.zip
├── data-analysis.zip     ← 一个独立技能包，规则同单个上传
├── contract-audit.zip
└── ...
```

批量成功响应示例

```
{
    "total": 2,
    "success": 1,
    "failed": 1,
    "results":[
        {
            "fileName":"data-analysis.zip",
            "success":true,
            "name":"data-analysis",
            "skillId":"a1b2-c3d4-e5f6-7890",
            "status":"2",
            "error":null
        },
        {
            "fileName":"broken.zip",
            "success":false,
            "name":null,
            "skillId":null,
            "status":null,
            "error":"无法解析该压缩包"
        }
    ]
}
```

### 3.获取统计信息✅

●方法: GET
●路径: /api/admin/stats
●说明: 返回技能总数、下载总量和分类列表。`total_skills` 与 `total_downloads` 均按**技能市场展示口径**统计（`status IN ('2','4')`：已上架 + 待下架审核，且未彻底删除）——待下架技能仍在市场可读，计入统计。`categories` 为全部启用的 category 维度取值名。

响应示例

```
{
    "total_skills": 6,
    "total_downloads": 6,
    "categories":[
        "audit",
        "data-analysis"
    ]
}
```

### 4.更新技能分类和标签✅

●方法: PATCH
●路径: /api/admin/skills/{name}/tags
●说明: 更新技能的分类和标签。**仅允许修改「在架(status=2)」技能**——待下架审核(status=4)正处于下架流程中不可改，草稿/被驳回/已下架技能同理拒绝，命中不可改技能返回 404。
●请求类型: application/json

路径参数

| 参数名 | 类型   | 必填 | 说明                   |
| ------ | ------ | ---- | ---------------------- |
| name   | string | 是   | 技能ZIP文件，最大100MB |

请求体示例

```
{
    "tags":[
        "contract",
        "audit",
        "legal"
    ],
	"category":"audit"
}
```

响应示例

```
{
    "message":"Tags and category updated successfully",
    "name":"contract_audit",
    "tags":[
        "contract",
        "audit",
        "legal"
    ],
    "category":"audit"
}
```

### 5.提取技能元信息（不落库）

●方法: POST
●路径: /api/admin/meta/extract
●说明: 纯计算接口。传入 SKILL.md 全文，返回大模型翻译提取的中文显示名、中文描述与结构化概览，**不写库、不涉及任何已有技能**。用于第三方在创建/修改技能前预览提取结果。
●请求类型: application/json

请求体参数

| 参数名  | 类型   | 必填 | 说明             |
| ------- | ------ | ---- | ---------------- |
| content | string | 是   | SKILL.md 全文    |

请求体示例

```
{
    "content":"---\nname: contract-audit\ndescription: Audit contracts...\n---\n# Contract Audit\n..."
}
```

响应示例

```
{
    "skill_name":"合同审核助手",
    "display_description":"对上传的合同文本做结构化风险审核…",
    "detail_json":{
        "taskGoal":"识别合同中的风险条款",
        "taskDetails":[
            "逐条比对标准条款"
        ],
        "triggerKeywords":[
            "合同",
            "审核"
        ]
    }
}
```

说明与边界

- `detail_json` 为**解析后的 JSON 对象**；提取不到或内容非法时为 `null`。
- 大模型连接失败/降级时返回 **200**，对应字段为空串、`detail_json` 为 `null`（空结果是合法回答，由调用方决定是否重试）。
- `content` 缺失或为空白返回 **400**。
- 本接口需实时调用大模型，响应耗时可达**数十秒**，调用方 HTTP 超时需相应放宽。

### 6.重新翻译提取并保存

●方法: POST
●路径: /api/admin/skills/{name}/re-extract
●说明: 对已有技能重新执行「翻译 + 详细信息提取」并落库。用于创建技能时因大模型连接失败导致显示名/描述/概览为空的**元数据修复**场景。**仅允许修改「在架(status=2)」技能**（与「更新技能分类和标签」同口径），非在架或不存在返回 404。
●请求类型: 无请求体

路径参数

| 参数名 | 类型   | 必填 | 说明             |
| ------ | ------ | ---- | ---------------- |
| name   | string | 是   | 技能唯一标识     |

响应示例

```
{
    "name":"contract_audit",
    "skill_name":"合同审核助手",
    "display_description":"对上传的合同文本做结构化风险审核…",
    "detail_json":{
        "taskGoal":"识别合同中的风险条款"
    },
    "updated_fields":[
        "skill_name",
        "display_description",
        "detail_json"
    ],
    "rows_updated":1
}
```

落库规则

- 数据来源为该技能**在架行的 SKILL.md**，无需传入内容。
- **只写非空字段**：大模型某个字段提取失败时跳过该字段，不会用空值覆盖已有数据。`updated_fields` 明确列出本次实际写入的字段。
- 同时写入技能主表与版本快照，站内技能市场与对外接口**立即生效**。
- 不修改草稿行，避免覆盖创建者正在编辑的新版本。
- 不修改 `name`（技能唯一标识）与英文 `description` 原文。

错误响应

| 状态码 | 场景                                             |
| ------ | ------------------------------------------------ |
| 404    | 技能不存在，或该技能无在架(status=2)行            |
| 400    | 在<br/>架行无 SKILL.md 内容，无法提取                  |
| 502    | 大模型不可用，三个字段全部提取为空，**未落库**，可重试 |

本接口同样需实时调用大模型，响应耗时可达**数十秒**。

## 外部审核结果接口

面向第三方平台：轮询获取其用户提交技能的**上架/下架审核结果**事件。仅对通过「上传技能包」时携带 `platform + external_user_id` 的外部上传技能产生记录。**无鉴权**（与其它 `/api/*` 对外接口一致）。

### 1.获取外部审核结果通知

●方法: GET
●路径: /api/external/notifications
●说明: 按第三方平台与其用户标识，拉取该外部用户的技能审核结果事件流。默认返回**最新 N 条**；对能在自身侧记住游标的平台，可用 `since_id` 增量拉取。

查询参数

| 参数名           | 类型    | 必填 | 默认值 | 说明                                                         |
| ---------------- | ------- | ---- | ------ | ------------------------------------------------------------ |
| platform         | string  | 是   | /      | 第三方平台标识（须与上传时一致）                             |
| external_user_id | string  | 是   | /      | 第三方用户唯一标识（须与上传时一致）                         |
| limit            | integer | 否   | 20     | 返回条数上限；`<=0` 按默认 20，超过 **200** 收敛为 200       |
| since_id         | integer | 否   | /      | 增量游标：传入后只返回 `id > since_id` 的记录（仍受 limit 封顶） |

行为约束

●默认（不传 `since_id`）按 `id` **倒序**返回该外部用户最新的 `limit` 条；无需第三方持久化。
●`max_id` 为本次返回结果中的最大 `id`；无结果时为 `0`。能记游标的第三方可在下次请求携带 `since_id=max_id` 增量拉取，天然去重。
●**注意（突发超 limit 可能漏事件）**：`since_id` 增量模式按 `id` 倒序返回，若单个外部用户在两次轮询之间新增的审核结果**超过 limit**，将 `since_id` 推进到 `max_id` 会静默跳过本窗口内更早的未返回事件。调用方应在未追平时**持续轮询或调大 limit**，直至返回空列表。
●**注意（audit_comment 外泄）**：审核意见原文 `audit_comment` 会经本**无鉴权**接口原样返回给第三方；审核人不应在驳回意见中写入敏感内部信息。

响应字段说明（notifications 数组项）

| 字段          | 类型    | 说明                                                    |
| ------------- | ------- | ------------------------------------------------------- |
| id            | integer | 事件ID（自增，单调递增，兼作游标）                      |
| name          | string  | 技能唯一标识（对应上传返回的 name，供第三方对应）       |
| skill_name    | string  | 技能显示名                                              |
| skill_id      | string  | 逻辑 skill 标识                                         |
| scene         | string  | 审核场景：`1`-上架 `2`-下架                             |
| result        | string  | 审核结果：`1`-通过 `0`-驳回                             |
| audit_comment | string  | 审核意见（可为空）                                      |
| create_time   | string  | 事件时间，格式 `yyyy-MM-dd HH:mm:ss`                    |

请求示例

```
# 默认拉取最新 20 条
GET /api/external/notifications?platform=acme&external_user_id=u-777

# 增量拉取 id 大于 1024 的、最多 5 条
GET /api/external/notifications?platform=acme&external_user_id=u-777&since_id=1024&limit=5
```

响应示例

```
{
    "notifications":[
        {
            "id": 1050,
            "name": "pdf-tools",
            "skill_name": "PDF 工具",
            "skill_id": "abc123",
            "scene": "1",
            "result": "0",
            "audit_comment": "缺少 LICENSE",
            "create_time": "2026-07-08 11:00:00"
        },
        {
            "id": 1024,
            "name": "csv-helper",
            "skill_name": "CSV 助手",
            "skill_id": "def456",
            "scene": "1",
            "result": "1",
            "audit_comment": "通过",
            "create_time": "2026-07-08 10:00:00"
        }
    ],
    "max_id": 1050
}
```

## 常见错误响应

```
{
    "detail":{
        "error":"Skill not found",
        "name":"unknown-skill"
    }
}


{
    "detail":{
        "error":"File not found",
        "path":README.md"
    }
}
```
