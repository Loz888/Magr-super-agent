# SA22 飞书回复后端

用户已明确要求继续接通 Bot 群回复。当前切片为原消息下的确定性接收/状态回复，
不调用模型、不接纳编码任务、不开放 PR/merge/部署；H12 原 FAIL 保留。
是否已经实际通过以本卡分版本检查及平台回执记录为准，本文不是验收通过结论。

`source_replies.queue_reply` 要求当前 Owner、真实当前 Source 输入、明确 @、人类
requester/context_provider，以及 auto_investigate 下的独立通知许可。
Admission/Execution 关闭、model_input=false；默认 Shadow 仍然不发送。
服务端只为该安装/群/话题签署 verified_result/public_summary、512字节、20条/小时
及有界期限的通知配置，正文固定说明已经收信和自动编码未启用。

控制回复以 source-receipt-reply-v1 标记，复用原 Thread/Mandate/Ticket/Run 和
Action/Approval/Outbox/Dispatcher。原输入有唯一外部键，并发/重投复用同一回复。
这是接收回执工作流，不能计入真实编码成功任务；不会产生 Coding Mandate、
Assignment、Job、验证通过的补丁或付费请求。计划失败与原记录在同一 Engine 事务回滚。
没有迁移、共享 Engine/API/预算算法或 Harness 活动树改动。

现有 Task 通知保留原模板与事实 hash。live profile 只接受服务端显式构造的
FeishuNotificationChannel；受控 MockTransport 只能配 fixture。
真实发送按原输入的远端 message_id 调用飞书 reply API，携带确定性的 UUID；
调用发生在业务 SQL 事务之外。令牌请求后再次核对当前策略、身份、源版本、
原 Action/hash/状态、凭据和目的地，再发消息。仅向当前原会话发送固定文本，
不回显原文、日志、代码或任意 URL，也不扩大群历史权限。

明确拒绝记为失败；发送超时、响应丢失或不完整响应为 unknown，原 Outbox 不重发。
没有可靠的 UUID 查询权限/能力，query_delivery=unsupported；平台幂等能力未独立
实测，声明 unknown。既有本地去重/冷却仍生效，平台 UUID 不作为盲目重发依据。

登记检查：`make verify TASK=SAV1-22-REPLIES`，仅执行本切片新用例。
失败只复验失败节点，旧通过项不重跑。检查使用专属临时 PG 和 MockTransport，
不会操作仍44迁移的主库或实际接入 PG。受控结果不代替实际群回复、H12 或完整 SA22 放行。
