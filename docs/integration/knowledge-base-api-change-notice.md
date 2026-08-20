# 知识库 BFF 调用方迁移通知（历史）

**状态：** 已完成，历史通知；新接口与字段说明请使用
[知识库 BFF 使用说明](integration-knowledge-base-api.md#接口迁移) 或运行中的 `GET /docs`。

知识库 BFF 已从 WebUI 旧别名切换为下游同名路径。所有普通接口均使用：

```text
POST /api/integration/knowledge_base/<下游知识库接口名>
```

旧路径已不再支持；完整旧新路径对照表以 API 文档的「接口迁移」章节为准。调用方应
使用对应下游接口名作为最后一级路径；请求体和响应体均原样转发。

`upload_artifacts` 是 WebUI 本地 workspace 文件编排接口，不属于下游同名透传；其余
接口均遵循上述规则。
