# Campaign 异步回合等待

`scripts/real_model_campaign.py` 现在会在父聊天流收到
`background_task_dispatched` 后，订阅该会话的后台任务事件。脚本会等待本轮
委派进入聚合完成状态；若服务端发出 `server_turn_started`，还会消费对应的
wakeup 聊天流。只有本轮后台任务已结算、被明确标为未解析，或等待超时后，
才继续进行 Manifest 对齐或发送下一轮消息。

等待过程和唤醒流事件会记录在 campaign 结果中；未解析、唤醒流错误和超时会
作为本轮对齐失败保存，避免把未完成的异步工作误判为已完成。

CAMPAIGN_ID=20260910-133557-4eda1776
SESSION_ID=4b4ca656cfde
TURN=5
NONCE=537add9f48e4431cb7224f257bbbee11
