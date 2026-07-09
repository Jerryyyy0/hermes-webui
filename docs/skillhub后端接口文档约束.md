# 接口文档
以下内容来源于项目内的Markdown文档文件，可直接维护并同步展示。该文档作为对外暴露接口的约束文档，里面涉及到的接口必须严格按照接口文档的要求开发。

## 基础信息

●基础地址: http://localhost:8000
●接口前缀: /api/skills、/api/admin
●数据格式:除下载接口外，默认返回 application/json
●字符编码: UTF-8

## 状态码约定

200:请求成功
400:请求参数错误或上传文件不符合要求
404:资源不存在
500:服务端处理失败

## 技能接口

### 1.获取分类列表✅

●方法: GET
●路径: /api/skills/categories
●说明: 返回当前系统中的所有唯一分类

响应示例

```
[
"audit",
"data-analysis"
]
```

### 2.获取技能列表

●方法: GET
●路径: /api/skills
●说明: 分页查询技能列表，支持关键字和分类筛选

查询参数

| 参数名    | 类型    | 必填 | 默认值 | 说明                 |
| --------- | ------- | ---- | ------ | -------------------- |
| q         | string  | 否   | /      | 搜索关键字           |
| category  | string  | 否   | /      | 分类筛选             |
| page      | integer | 否   | 1      | 页码，从1开始        |
| page_size | integer | 否   | 20     | 每页数量，范围 1-100 |

请求示例

```
GET  /api/skills?q=data&page=1&page_size=9
```

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
    "total": 1,
    "page": 1,
    "page_size": 9
}
```

### 3.获取单个技能信息

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
| category            | string         | 分类名称                                                     |
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

### 4.下载技能包

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

### 5.获取技能文档

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

### 6.获取技能目录结构

●方法: GET
●路径: /api/skills/{name}/structure
●说明: 返回技能目录结构，按文件**所在顶层目录**归类（不再按扩展名）：
  - `root`：根目录文件（如 `SKILL.md`、`LICENSE.txt`）
  - `scripts`：`scripts/` 目录下的全部文件（含子目录，如 `scripts/__pycache__/*.pyc`）
  - `references`：`references/` 及 `assets/` 等其它资源目录下的文件
  - 目录占位项（is_directory=1）不返回

路径参数

| 参数名 | 类型   | 必填 | 说明     |
| ------ | ------ | ---- | -------- |
| name   | string | 是   | 技能名称 |

响应示例

```
{
    "name":"data-analysis",
    "root":[
        {
            "name":"SKILL.md",
            "path":"SKILL.md"
        },
        {
            "name":"LICENSE.txt",
            "path":"LICENSE.txt"
        }
    ],
    "scripts":[
        {
            "name":"run.py",
            "path":"scripts/run.py"
        }
    ],
    "references":[
        {
            "name":"README.md",
            "path":"references/README.md"
        }
	]
}
```

### 7.获取技能文件内容

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

### 8.删除技能

●方法: DELETE
●路径: /api/skills/{name}
●说明: 删除指定技能及其相关资源

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

### 1.刷新技能索引

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
●说明: 上传ZIP文件并自动解压到技能目录，要求压缩包内包含SKILL.md。上传后会**自动调用大模型补全技能元信息**（与页面上传同一套能力）并**自动发起上架审核**。
●请求类型: multipart/form-data

表单参数

| 参数名  | 类型    | 必填 | 说明                                                         |
| ------- | ------- | ---- | ------------------------------------------------------------ |
| file    | file    | 是   | 技能ZIP文件，最大100MB                                       |
| user_id | integer | 否   | 归属用户ID；为空默认归属 admin，非空归属对应用户（用户不存在则拒绝）。审核人始终为 admin |

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

成功响应示例

```
{
    "message":"Skill uploaded and submitted for listing audit",
    "name":"data-analysis",
    "skillName":"数据分析",
    "skillId":"a1b2-c3d4-e5f6-7890",
    "status":"1"
}
```

失败响应示例

```
{
	"detail":"Only ZIP files are allowed"
}
```

### 3.获取统计信息

●方法: GET
●路径: /api/admin/stats
●说明: 返回技能总数、下载总量和分类列表

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

### 4.更新技能分类和标签

●方法: PATCH
●路径: /api/admin/skills/{name}/tags
●说明: 更新技能的分类和标签
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
