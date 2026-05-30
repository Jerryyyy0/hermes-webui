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

### 1.获取分类列表

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

响应示例

```
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
●说明: 返回技能目录中的脚本文件和参考资料列表

路径参数

| 参数名 | 类型   | 必填 | 说明     |
| ------ | ------ | ---- | -------- |
| name   | string | 是   | 技能名称 |

响应示例

```
{
    "name":"data-analysis",
    "scripts":[
        {
            "name":"run.py",
            "path":"run.py"
        }
    ],
    "references":[
        {
            "name":"README.md",
            "path":"README.md"
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

### 2.上传技能包？

●方法: POST
●路径: /api/admin/upload
●说明: 上传ZIP文件并自动解压到技能目录，要求压缩包内包含SKILL.md
●请求类型: multipart/form-data

表单参数

| 参数名 | 类型 | 必填 | 说明                   |
| ------ | ---- | ---- | ---------------------- |
| file   | file | 是   | 技能ZIP文件，最大100MB |

约束说明

●文件名扩展名必须为.zip
●技能名取自文件名，不包含扩展名
●技能名只允许字母、数字、连字符和下划线
●压缩包内必须包含 SKILL.md

成功响应示例

```
{
    "message":"Skill uploaded successfully",
    "name":"data-analysis"
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

### 4.更新技能分类和标签？

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
