# SA00 架构决定提案（未分配正式 ADR 编号）

状态：PROPOSED，等待 Harness H01 的稳定接口和选型 ADR 后冻结；不更改现有运行行为。

1. **来源领域版本化。** 新 SourceEvent/SourceThreadRef/ActorRef 与旧 TriggerInput 通过适配兼容。
   安装/租户和 opaque ID 取自可信事件；正文、显示名和模型推断不绑定身份。未关联仓库的
   事件进入受控来源收件箱；无话题群按问题关联，不以群 ID 或虚构 Issue 编号充当任务。
2. **四层许可。** Receive/Admission/Execution/Notification 独立。团队规则由管理员预先签署，
   且含 scope、期限、模式、预算和角色；Memory、日志、订阅与 Router 均不能扩权。
3. **旧 R3 的正式扩展。** 保留旧显式请求协议；新版本才允许明确团队规则下的自然信号、
   动态订阅、TTL、handback/unsubscribe。投递、控制、恢复各自重验当前权限，保留旧决定。
4. **唯一业务权威。** 复用 Engine 的持锁连接、现有 Thread/Ticket/Run/Mandate/Outbox；
   Source、Router、Scheduler、Harness Worker 和 Watchdog 都不另建可裁决业务的写库。
5. **通知预授权与精确发布分开。** 常规结果通知可由具体 NotificationGrant 校验实际内容
   hash 后生成 Action；Draft PR 仍绑定完整 Diff/tree/payload_hash 和已认证操作者审查。
   普通 IM 文本、auto_repair 模式及平台接收权限均不替代发布审批。
6. **预算、撤权与未知结果。** Router/Recall/验证/Worker 共享项目总额；新事件/Run/跨日
   不重置未知预占。策略 revision 与 execution epoch 变化阻止旧操作；已发请求按真实状态
   结算，对外 unknown 先只读核对，不能按超时目标盲重试。
7. **版本与数据生命周期。** PR SHA、工作树、输入 revision、权限版本与验证/审批绑定；
   撤权、删除和保留期影响来源正文、模型审计、Memory/CAS、缓存、导出、备份恢复与待发内容。

本提案的正式编号、迁移编号与共享模块负责人需要按 COORDINATION.md 与 Trae 对齐。
不提前抢占编号或实现第二套 Harness；兼容测试、负例和放行证据在相应实施卡逐项建立。
