# 知识库 BFF 路由末段与下游接口名对齐方案

**状态：** 已实施

## 目标与范围

将 WebUI 知识库 BFF 的公开路由统一为：

```text
POST /api/integration/knowledge_base/<下游知识库接口名>
```

本方案将 BFF URL 的最后一层路径改为下游接口名，并将普通 JSON 请求体改为原样
透传。下游请求方法、下游路径前缀 `/knowledge_base/`、超时、二进制透传、下游
HTTP 状态码和响应体透传规则均不变。

本仓库内没有生产前端调用这 10 个旧别名；调用点只出现在文档和测试。因此这是对
外部调用方的 URL 破坏性变更，不影响 WebUI 内部对下游的实际调用。

## 调用链与现状

知识库请求目前经过以下链路：

```text
调用方
  -> /api/integration/knowledge_base/<WebUI 路由>
  -> integration.knowledge_base.handlers._route_key()
  -> integration.knowledge_base.client.post_*()
  -> {KNOWLEDGE_BASE_URL}/knowledge_base/<下游路由>
```

`integration/knowledge_base/constants.py` 中的 `DOWNSTREAM_PATHS` 是当前别名到
下游接口名的唯一映射来源。不要在每个 handler 分支中复制路由翻译逻辑。

## 完整接口清单

### 需要改名的 10 个 BFF 路由

| 现有 WebUI 路由末段 | 下游知识库接口名 | 实施后的 WebUI 路由末段 |
| --- | --- | --- |
| `list` | `list_ps_knowledge_bases` | `list_ps_knowledge_bases` |
| `joined` | `user_joined_shkbs` | `user_joined_shkbs` |
| `create` | `create_ps_kb` | `create_ps_kb` |
| `info` | `show_ps_kb_info` | `show_ps_kb_info` |
| `edit` | `edit_kb_information` | `edit_kb_information` |
| `delete` | `delete_ps_kb` | `delete_ps_kb` |
| `available` | `available_shkbs` | `available_shkbs` |
| `apply_join` | `apply_join_shkb` | `apply_join_shkb` |
| `members` | `get_user_inshkb` | `get_user_inshkb` |
| `documents` | `list_knowledge_bases_details` | `list_knowledge_bases_details` |

例如，列表接口由：

```text
POST /api/integration/knowledge_base/list
```

改为：

```text
POST /api/integration/knowledge_base/list_ps_knowledge_bases
```

它仍会调用同一个下游地址：

```text
POST {KNOWLEDGE_BASE_URL}/knowledge_base/list_ps_knowledge_bases
```

### 已经同名的 14 个下游端点

以下 BFF URL 已经与下游接口名一致，不应修改：

| BFF 路由末段 | 下游知识库接口名 | 处理方式 |
| --- | --- | --- |
| `upload_docs` | `upload_docs` | multipart 原样透传 |
| `update_docs` | `update_docs` | JSON 原样透传 |
| `delete_docs` | `delete_docs` | JSON 原样透传 |
| `show_pdf` | `show_pdf` | JSON 或二进制透传 |
| `search_docs` | `search_docs` | JSON 原样透传 |
| `search_docs_xcore` | `search_docs_xcore` | JSON 原样透传 |
| `creater_handle_application` | `creater_handle_application` | JSON 原样透传 |
| `get_joinkb_applications` | `get_joinkb_applications` | JSON 原样透传 |
| `get_user_messages` | `get_user_messages` | JSON 原样透传 |
| `mark_message_read` | `mark_message_read` | JSON 原样透传 |
| `user_exit_shkb` | `user_exit_shkb` | JSON 原样透传 |
| `remove_from_myshkb` | `remove_from_myshkb` | JSON 原样透传 |
| `delete_readed_message` | `delete_readed_message` | JSON 原样透传 |
| `download_doc` | `download_doc` | JSON 或二进制透传 |

### 保留的 WebUI 编排路由

`POST /api/integration/knowledge_base/upload_artifacts` 没有同名下游接口，必须保留。
它从受信任 workspace 读取文件、执行数量和大小限制，再以 multipart 调用下游
`upload_docs`。将它改成 `upload_docs` 会与现有的原始 multipart 透传接口冲突，并会
丢失 workspace 编排语义。

### 通知模块的下游调用

`integration/notifications/handlers.py` 不通过 BFF URL 回环，而是直接使用知识库
client。它调用的下游接口均已同名，不需要改调用参数或 URL：

| 下游接口名 | 调用用途 |
| --- | --- |
| `get_user_messages` | 已读 ID 查询、通知列表、通知汇总 |
| `mark_message_read` | 批量标记通知已读 |
| `delete_readed_message` | 批量删除通知 |

## 推荐实现

采用一次性切换：新 URL 只接受下游原始接口名；10 个旧别名不再注册。这样公开
契约可直接从 URL 推导下游接口，避免长期维护两套名称。

1. 在 `integration/knowledge_base/constants.py` 基于 `DOWNSTREAM_PATHS` 增加反向的
   `WEBUI_ROUTE_TO_ROUTE_KEY` 映射，即 `下游接口名 -> 现有内部 route_key`。
   该映射是从现有字典推导的，避免两份人工维护的 10 项映射。
2. 修改 `integration/knowledge_base/handlers.py` 的 `_route_key()`：
   - 从 URL 取最后一层路径；
   - `upload_artifacts` 继续直接返回 `upload_artifacts`；
   - 其余路由通过 `WEBUI_ROUTE_TO_ROUTE_KEY` 还原为内部 route key；
   - 旧别名不再识别。
3. 保持 `client.post_json()` 的内部 route key 不变，使其继续使用
   `DOWNSTREAM_PATHS` 组成实际下游 URL；移除普通 JSON 路由的字段校验、构造和默认
   值注入，直接将解析后的 JSON 对象传给 client。`upload_artifacts` 继续保留本地
   参数校验和安全路径检查。
4. 更新对外契约：
   - `integration/swagger/openapi.json` 中上述 10 个 path key；
   - `integration/README.md` 的知识库 BFF 表格和列表接口 curl 示例；
   - `docs/integration/integration-knowledge-base-api.md` 的示例、章节路径和汇总 curl；
   - 若文档中存在这 10 个旧 URL 的其它引用，同步替换。
5. 不更新根目录 `CHANGELOG.md`。这是 Fork integration 行为变更；实施发布说明已写入
   `integration/CHANGELOG.md`。

## 兼容性决策与迁移

推荐的默认决策是破坏性切换，发布时让门户或其他外部调用方将上述 10 条 URL 一并
改为新路径。切换前应向调用方提供本文件中的映射表和发布日期。

若无法同步升级外部调用方，可改用限期兼容方案：在反向映射外另建一个明确命名的
`LEGACY_WEBUI_ROUTE_TO_ROUTE_KEY`，只接受这 10 个旧名，并在文档中给出删除版本和
日期。不要把旧别名混入 `DOWNSTREAM_PATHS`，否则公开 URL 与下游接口名的主契约又会
变得不明确。是否保留兼容别名需要在实施前确认；本方案的默认实现不保留。

## 测试与验收

1. 在 `integration/tests/knowledge_base/test_handlers.py` 将已有别名 URL 测试改为
   新 URL，并新增参数化测试：`DOWNSTREAM_PATHS` 中每个下游接口名都能解析到正确的
   内部 route key；`upload_artifacts` 仍可解析。
2. 增加旧别名拒绝测试，证明例如 `/api/integration/knowledge_base/list` 不再由知识库
   handler 处理。这条测试在实现前应失败，新 URL 解析测试也应在实现前失败。
3. 保留并运行 `integration/tests/knowledge_base/test_client.py`，确认新公开 URL 的
   路由归一化没有改变最终下游地址、payload、30 秒默认超时及 180 秒文件接口超时。
4. 运行通知测试，确认直接使用 `get_user_messages`、`mark_message_read` 和
   `delete_readed_message` 的聚合行为未受影响。
5. 更新 Swagger 后运行其测试，确认 `/docs` 中只出现新的 10 条 URL。

建议执行：

```bash
./scripts/test.sh integration/tests/knowledge_base integration/tests/notifications integration/tests/swagger
```

在配置隔离状态和测试知识库地址后，手动验证至少一个 JSON 原样透传接口、一个 multipart
接口和一个二进制接口：

```text
list_ps_knowledge_bases -> 请求体与调用方提交内容一致
upload_docs             -> multipart 原样透传不变
download_doc            -> 文件响应和 Content-Disposition 透传不变
```

验收标准是：10 个新路径均到达与旧路径相同的下游接口并保持原有响应；14 个同名
路径、`upload_artifacts` 和通知聚合没有回归；旧路径的行为符合已确认的兼容性决策。

## 预计改动文件

| 文件 | 改动 |
| --- | --- |
| `integration/knowledge_base/constants.py` | 增加从公开下游接口名到内部 route key 的派生映射，并删除普通 JSON 路由默认值常量 |
| `integration/knowledge_base/handlers.py` | 用派生映射解析 BFF URL 最后一层路径，并将普通 JSON body 原样转发 |
| `integration/knowledge_base/client.py` | 删除不再使用的普通 JSON 字段构造函数 |
| `integration/tests/knowledge_base/test_handlers.py` | 更新 URL fixture，添加新名可达与旧名拒绝覆盖 |
| `integration/tests/knowledge_base/test_client.py` | 删除字段构造函数测试，保留最终下游 URL 与原始 body 的断言 |
| `integration/swagger/openapi.json` | 重命名 10 个公开 path key，并将普通 JSON 请求 schema 改为透传对象 |
| `integration/README.md` | 更新 BFF 路由表、curl 示例和原样参数透传说明 |
| `docs/integration/integration-knowledge-base-api.md` | 更新接口章节、汇总示例和下游参数契约说明 |
| `integration/CHANGELOG.md` | 增加 Fork 侧发布说明 |
