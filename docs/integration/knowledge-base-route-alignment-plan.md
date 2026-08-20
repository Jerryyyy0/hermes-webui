# 知识库 BFF 路由对齐决策记录

**状态：** 已实施，历史记录；当前接口契约请见
[知识库 BFF 使用说明](integration-knowledge-base-api.md) 与 Swagger。

## 决策

WebUI 公开路由统一为：

```text
POST /api/integration/knowledge_base/<下游知识库接口名>
```

普通 JSON 接口的请求体、下游 HTTP 状态码和 JSON 响应体均原样转发。旧的
`/list`、`/joined`、`/create` 等 WebUI 别名不再提供。

## 实施结果

- `DOWNSTREAM_ROUTE_NAMES` 是可转发下游接口名的唯一白名单。
- 公开 URL、WebUI 内部路由名与下游最后一级路径保持同名，不保留内部别名映射。
- `upload_docs` 保持 multipart 原样透传；`show_pdf`、`download_doc` 保持二进制透传。
- `upload_artifacts` 是唯一 WebUI 本地编排接口：读取 workspace 文件后调用下游
  `upload_docs`，不属于普通透传接口。
- 知识库通知模块直接调用下游 `get_user_messages`、`mark_message_read` 与
  `delete_readed_message`，不经 BFF URL 回环。

## 验证

```bash
./scripts/test.sh integration/tests/knowledge_base integration/tests/notifications integration/tests/swagger
```

旧 URL 拒绝、同名 URL 可达、请求体原样转发及文件响应透传均由知识库测试覆盖。
