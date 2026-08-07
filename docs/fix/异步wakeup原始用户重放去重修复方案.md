# 异步 Wakeup 原始 User 重放去重修复方案

- **状态：** Implemented（仅 WebUI 显示层）
- **创建日期：** 2026-08-06
- **适用范围：** WebUI 异步委派完成后的历史 session transcript、streaming merge 与显示投影

## 1. 问题定义

异步委派完成时，WebUI 会把原始 completion 正文作为隐藏的模型上下文输入，例如：

```text
[ASYNC DELEGATION BATCH COMPLETE - deleg_267a7280]
```

Agent 为了理解该结果，会重新加载原始对话历史。历史中的真实 user 可能被复制成新的
Python 字典。复制后的字典没有原来的 `_DB_PERSISTED_MARKER`，于是 Agent 的 flush 逻辑
把它误判为新 user 并再次写入数据库：

```text
turn:8 user "调研模型动态"
turn:8 user "调研模型动态"   # history replay 副本
```

第二条不是用户的新输入，也不是异步子任务生成的业务消息，而是上下文重放副本被错误持久化。

## 2. 修复原则

1. 不修改 Agent 原有自动注入消息的 `role` 和 `content`。
2. 不修改 `[ASYNC DELEGATION BATCH COMPLETE ...]` 的原始正文。
3. 不改变 Agent 的回补、恢复和模型上下文构造流程。
4. 只在 WebUI 显示投影增加身份标识与去重保护。
5. 不按 `role + content` 全局去重，避免误删用户主动重复发送的消息。

## 3. 消息分类

异步回补上下文包含两种不同消息：

```text
已有历史：
user: 原始用户问题                         turn:8

本次新增：
user: [ASYNC DELEGATION BATCH COMPLETE ...] context_anchor
```

`context_anchor` 是新的 Agent 上下文输入，应继续提供给模型，但不出现在 WebUI transcript、
普通 SSE `message` 事件或 Manifest 的 user turn 中。

原始 user 的 replay 副本不是新的 turn，必须与原始 user 使用同一个稳定身份。

## 4. 原始 User 的后台任务标识

成功派发 `delegate_task` 后，在原始 user 的 WebUI 显示投影上增加最小标识：

```json
{
  "role": "user",
  "content": "派发后台任务",
  "_turn_key": "turn:8",
  "_background_task_ids": ["deleg_123"]
}
```

字段约束：

- `_background_task_ids` 只包含成功派发的 `delegation_id`；
- 同一 turn 可以包含多个后台任务 ID；
- 原始 `role`、`content` 和 `_turn_key` 不改变；
- 该字段是 WebUI 内部显示/归属元数据，不发送给 Provider；
- 任务状态、完成时间和 `wakeup_state` 仍由 sidecar 的
  `delegation_id -> origin_turn_key` 记录管理，不塞进 user message；
- 派发失败或没有 `delegation_id` 时不增加该字段。

这个字段只表示“该 turn 发起过后台任务”，不负责给孤立 assistant 分配 turn。孤立 assistant
仍必须通过 `delegation_id` 查 sidecar 的 `origin_turn_key`，再在历史显示投影中补上：

```json
{
  "role": "assistant",
  "content": "后台任务最终回复",
  "_turn_key": "turn:8",
  "_source": "async_delegation_wakeup",
  "delegation_id": "deleg_123"
}
```

## 5. WebUI 显示去重方案（不改变 Agent 回补机制）

### 5.1 稳定身份

优先使用现有的真实 turn 标识：

```text
display_identity = (session_id, role=user, _turn_key)
```

不引入新的数据库幂等字段或唯一索引。当前显示层使用已有的 `_turn_key`；不能使用正文
作为唯一身份。

### 5.2 显示投影规则

在分页、`msg_limit`、`msg_before`、`turn_align` 和 streaming merge 之前执行：

```text
1. context_anchor/internal_scaffold：直接隐藏
2. 同一 session、同一 _turn_key 的真实 user：保留最初的 canonical user
3. 不同 _turn_key：全部保留，即使正文完全相同
4. 缺少稳定身份的历史 user：不猜测、不删除
```

同一 turn 出现重复 user 时，不能简单保留最新数据库行。应按数据库插入顺序保留最初的
真实 user，并从 replay 副本合并缺失的 WebUI 元数据：

```text
id=100  turn:8  "你好"  用户真实发送，保留
id=120  turn:8  "你好"  Agent replay 副本，隐藏
```

如果 `id=100` 已包含：

```json
"_background_task_ids": ["deleg_123"]
```

则继续保留在 canonical user 上。replay 副本不能覆盖原始时间、来源或 turn 归属。

用户之后主动再次发送相同正文时会产生新的 turn，必须保留：

```text
id=100  turn:8  "你好"  -> 显示
id=120  turn:8  "你好"  -> 隐藏 replay 副本
id=130  turn:9  "你好"  -> 显示，是真实的新一轮
```

例如：

```text
turn:8 user "你好"
turn:8 user "你好"       -> WebUI 显示一条

turn:8 user "你好"
turn:9 user "你好"       -> WebUI 显示两条
```

这里的“合并”只作用于 WebUI 的显示副本：保留 canonical user、合并必要元数据、隐藏
replay 副本；不删除数据库行，也不修改 Agent 模型上下文。

### 5.3 接入位置

- `GET /api/session` 合并 state.db、sidecar 后，且在分页参数生效前过滤。
- `api/streaming.py::_merge_display_messages_after_agent_result` 同时过滤
  `previous_display`、`previous_context` 和 `result_messages`。
- SSE 普通 user/assistant 投影、replay、recovery、导出和分享 transcript 统一复用同一 helper。
- 候选循环再次跳过已经出现的同 turn replay user，保留最终 assistant、tool、MEDIA 和 artifact。

本次实现的核心 helper 是
`integration.agent_message_semantics.projection.drop_non_display_messages()`：它先隐藏
内部 scaffold/context anchor，再按同一 `_turn_key` 隐藏后续 `role=user` replay。`GET
/api/session` 在计算 `msg_limit`、`msg_before` 和 `turn_align` 窗口前调用该投影，因此分页不会把
replay 当成一条可见消息；同时根据 session sidecar 的
`async_delegation_origins` 给原始 user 显示副本补 `_background_task_ids`。异步 wakeup 写回的
assistant/tool 显示副本补 `_turn_key`、`_source=async_delegation_wakeup` 和
`delegation_id`。数据库原始行和 Agent 模型上下文不变。

### 5.4 日志

每次过滤输出完整正文，便于确认是否误判：

```text
hermes_message_semantics
action=display_duplicate_drop
class=ordinary_user
kind=async_origin_user_replay
role=user
content='你好'
session_id=...
turn_key=turn:8
```

## 6. 历史数据与兼容策略

- 不删除已有重复数据库行。
- 不执行 `rebind_manifest_turn`，不迁移旧 artifact store。
- 没有稳定身份的历史普通 user 不按正文猜测隐藏。
- 旧 synthetic flag 继续兼容识别。
- `_background_task_ids` 只在成功派发后台任务的原始 user 显示投影中增加。
- 新产生的异步 wakeup assistant/tool 在写回时补显示身份；已有历史 assistant 不执行回溯重绑。
- 历史消息没有稳定身份时保持原样，不执行历史 rebind 或数据库清理。

## 7. 测试与验收

### WebUI

- 同一 `_turn_key` 的重复 user 只显示一次。
- 不同 `_turn_key` 的相同正文全部保留。
- 成功派发的原始 user 返回 `_background_task_ids`，失败派发不返回该字段。
- 同一 turn 的多个后台任务 ID 可以同时返回，且不改变 user 正文。
- completion anchor 不出现在 `/api/session`、SSE、分页、回放和导出。
- 最终 assistant、工具、MEDIA 和 artifact 仍然保留并归属原始 turn；assistant 的 `_turn_key`
  由 `delegation_id -> origin_turn_key` 恢复，而不是由 `_background_task_ids` 猜测。

### 手动验证

```text
turn:8 派发后台任务
completion 到达
turn:9 发送相同正文或新问题
刷新 /api/session
```

预期：`turn:8` 的原始 user 只显示一次；`turn:9` 的真实重复输入正常保留；completion
正文只作为隐藏模型上下文存在；后台最终 assistant 在时间线尾部显示并仍绑定 `turn:8`。

## 8. 结论

本方案只解决 WebUI 查询历史时的重复显示：不改 Agent 回补机制，不增加 Agent 数据库字段，
不创建唯一索引，不删除历史数据库行。WebUI 保留最初 canonical user，隐藏同 turn 的 replay
副本，并继续保留不同 turn 的相同正文。
