# ADR SAV1-00：来源、团队规则与通知的版本化权威

状态：CANDIDATE，待 H01 修复提交独立复验后接受用于实施；本 ADR 本身不启动来源、模型、Worker 或通知。

## 背景

旧 ADR0003 仅允许显式请求，并拒绝 handback/unsubscribe；ADR0012 的主动策略是独立只读 Run，不能授权普通代码修复。TriggerInput 的三种来源、正数 Issue 与 ASCII actor 也不能直接表达飞书安装内的身份、无仓库消息或原生话题缺失。沿用这些边界，同时以新版本增加 Source 与四层策略，避免暗改旧许可。

## 决定

1. SourceEvent v1、SourceThreadRef、ActorRef 在 `sources/` 定义；不确定仓库的事件进入受控来源收件箱，不使用伪 Issue。安装、租户和 opaque ID 来自验签平台事件。旧 TriggerInput 保持原契约；SA02 将来源身份映射成内部 Engine principal，保留原始 ID 大小写。
2. Receive、Admission、Execution、Notification 独立。管理员可预先签署有范围、有效期、角色、模式、版本及总预算的团队规则。只有新策略版本允许非显式信号调查/隔离修复；旧 R3/R6 grant 不升级。shadow 包括显式 @ 请求，Worker/编辑/通知/发布均为零；语义评估仍需独立 Router 预算。
3. 新版 Router 仅提议 ignore/observe/attach/create/ask；Engine 重验 actor、范围、规则、revision、epoch、预算和资源。动态订阅的 TTL、context_only、unsubscribe/handback 是 SA11 的版本化扩展，旧 API 的拒绝行为保留。上下文、模型和 Memory 不产生控制权。
4. Engine 与它持有的同一持锁 PG 连接仍是唯一业务写者。保留 Thread/Ticket/Run/Mandate/Action/Approval/Usage；Source、Scheduler、Harness 和 Watchdog 提交命令/事实。LangGraph 仅管 Run 图状态，Redis 可重建。Source 收件箱、关联与租约是新增事实，不能成为第二套主状态机。
5. 采用 H01 的现有 Python 端口路线，固定契约版本与提交后集成。DSH 未接线保持 NOT_RUN_NO_NODE_RUNTIME。H01 只做接口/参考回路；SA00 仅依赖 H00/H01，真实修复仍需 H11/H12 及其工作区/Broker/编码等前置，不扩大或缩短 DAG。
6. 模型/Recall/验证/Worker/子任务共享项目预算；先预占后调用，未知结果不退还、不自动重发，新 Run 和午夜不重置。策略与目标 revision/epoch 的改变阻止新旧操作混用，已发调用继续真实核账。
7. 通知可由具体 NotificationGrant 预授权目的地、类别、频率、期限和模板。Engine 对实际内容 hash 和当前 ACL 建审批记录，再入既有 Action/Outbox。Draft PR 仍需已认证操作者审查完整 Diff/tree/payload_hash；普通聊天“同意”不产生精确发布授权。unknown 只读对账，无查询能力则等待人工。
8. 来源正文、附件、模型输入审计、Memory/CAS、缓存、导出、待发消息与备份恢复共同遵守撤权/删除/保留期。业务引用与最小无正文审计保留；恢复旧备份先应用 tombstone/撤权记录。

## 实施与兼容

SA01–SA08 可与 Harness 后续卡并行，先交付来源/控制契约和确定性 Shadow；SA09 等 H02，SA12 等 H03/H04，SA13 等 H11/H12，SA14 等 H09，SA15 等 H10，SA18 等 H08。共享 Engine/API/预算/前向迁移先协调；本轨道 ADR 用独立命名空间。不得改历史迁移。

旧 GitHub/CLI/Web、旧只读策略、Action/Outbox 与现有 scope 限制维持；新增 Source 入口灰度切换必须唯一入站所有者。版本化旧新协议不互相解释，未来扩大到第二项目按 SA24 验收。

## 后果与验证

首版冻结一个组织、管理员、项目、GitHub 仓库、飞书机器人和群；默认 shadow、禁发、预算零。具体真实安装/用户/模型/额度在激活 manifest 核对前保持 NOT_CONFIGURED，不能由本开发授权推导出真实监听或付费许可。私有仓库需另验读取、模型外发和结果可见性。

复用审计每项绑定当前源码符号/哈希与真实回归入口；六个系统能力门、失败分母、200 条路由集协议、10 个正向任务和 72h/72h/5 工作日要求独立记录。文档冻结和受控回归不替代真实来源、编码、发布或长期运行成绩；所有历史 FAIL/NOT_RUN 保持原文件不变。
