# 录制脚本接口文档

## 输入输出参数

### 1. 保存录制脚本

`POST /api/integration/record_scripts/save`

请求体 JSON：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `relate_name` | string | 是 | 前端生成的稳定资源 id |
| `script_json` | string | 是 | 录制脚本 JSON 字符串 |

响应 JSON：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | boolean | 是否成功 |
| `relate_name` | string | 资源 id |
| `script_json` | string | 脚本 JSON 字符串 |
| `has_csv` | boolean | 是否有关联 CSV |
| `updated_at` | number | 更新时间戳 |
| `size` | integer | `script.json` 字节数 |

### 2. 查询录制脚本列表

`GET /api/integration/record_scripts/list`

请求参数：无

响应 JSON：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | boolean | 是否成功 |
| `scripts` | array | 脚本列表 |

`scripts[]` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `relate_name` | string | 资源 id |
| `script_json` | string | 脚本 JSON 字符串 |
| `has_csv` | boolean | 是否有关联 CSV |
| `updated_at` | number | 更新时间戳 |
| `size` | integer | `script.json` 字节数 |

### 3. 修改录制脚本

`POST /api/integration/record_scripts/update`

请求体 JSON：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `relate_name` | string | 是 | 要修改的资源 id |
| `script_json` | string | 是 | 完整替换的脚本 JSON 字符串 |

响应 JSON：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | boolean | 是否成功 |
| `relate_name` | string | 资源 id |
| `script_json` | string | 更新后的脚本 JSON 字符串 |
| `has_csv` | boolean | 是否有关联 CSV |
| `updated_at` | number | 更新时间戳 |
| `size` | integer | `script.json` 字节数 |

### 4. 删除录制脚本

`POST /api/integration/record_scripts/delete`

请求体 JSON：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `relate_name` | string | 是 | 要删除的资源 id |

响应 JSON：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | boolean | 是否成功 |
| `relate_name` | string | 资源 id |

说明：只删除 `script.json`，不会删除关联 CSV。

### 5. 上传录制脚本关联 CSV

`POST /api/integration/record_scripts_csv/upload`

请求类型：`multipart/form-data`

WebUI 不设置该 multipart 上传的文件大小上限；反向代理或上游网络层仍可能设置限制。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `relate_name` | string | 是 | 关联的资源 id |
| `file` | file | 是 | CSV 文件 |

响应 JSON：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | boolean | 是否成功 |
| `relate_name` | string | 资源 id |
| `filename` | string | 固定为 `data.csv` |
| `size` | integer | CSV 文件字节数 |

### 6. 下载录制脚本关联 CSV

`GET /api/integration/record_scripts_csv/download`

Query 参数：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `relate_name` | string | 是 | 资源 id |

响应：

| 类型 | 说明 |
|---|---|
| `text/csv` | CSV 文件下载流 |

### 7. 删除录制脚本关联 CSV

`POST /api/integration/record_scripts_csv/delete`

请求体 JSON：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `relate_name` | string | 是 | 资源 id |

响应 JSON：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | boolean | 是否成功 |
| `relate_name` | string | 资源 id |

## 调用示例

保存录制脚本：

```bash
curl -sS -X POST 'http://127.0.0.1:8787/api/integration/record_scripts/save' \
  -H 'Content-Type: application/json' \
  -d '{"relate_name":"demo_flow","script_json":"{\"steps\":[]}"}'
```

查询录制脚本列表：

```bash
curl -sS 'http://127.0.0.1:8787/api/integration/record_scripts/list'
```

修改录制脚本：

```bash
curl -sS -X POST 'http://127.0.0.1:8787/api/integration/record_scripts/update' \
  -H 'Content-Type: application/json' \
  -d '{"relate_name":"demo_flow","script_json":"{\"steps\":[{\"name\":\"step1\"}]}"}'
```

删除录制脚本：

```bash
curl -sS -X POST 'http://127.0.0.1:8787/api/integration/record_scripts/delete' \
  -H 'Content-Type: application/json' \
  -d '{"relate_name":"demo_flow"}'
```

上传 CSV：

```bash
curl -sS -X POST 'http://127.0.0.1:8787/api/integration/record_scripts_csv/upload' \
  -F 'relate_name=demo_flow' \
  -F 'file=@data.csv'
```

下载 CSV：

```bash
curl -L -o data.csv \
  'http://127.0.0.1:8787/api/integration/record_scripts_csv/download?relate_name=demo_flow'
```

删除 CSV：

```bash
curl -sS -X POST 'http://127.0.0.1:8787/api/integration/record_scripts_csv/delete' \
  -H 'Content-Type: application/json' \
  -d '{"relate_name":"demo_flow"}'
```
