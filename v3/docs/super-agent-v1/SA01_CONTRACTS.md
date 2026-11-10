# SAV1-01：Source v1 契约与迁移

版本：`source-contract-v1` / `source-event-v1`。SA01 提供领域模型、注册与最小元数据持久层、兼容桥接和前向迁移；真实平台接入与完整入站协议在 SA04–SA06。整卡状态以 `accept/SAV1-01/accept.json` 为准。

## 身份、事件与能力

`sources/domain.py` 定义 SourceScope、SourceInstance、ActorRef、SourceThreadRef、SourceEvent、SourceMetadata、能力、健康及现有业务引用。内部 source_instance_id 用 UUID；外部 tenant/installation/conversation/actor/entity/delivery ID 保留原样和大小写。作用域键用结构化 JSON 的 SHA-256，不用可能有分隔符碰撞的拼接。actor 的 kind 不是身份键的一部分，也不产生角色权限；显示名不参与身份。

SourceThreadRef 区分 conversation 与 native_thread。没有原生话题时 topic_key 明确为 null，不把整个群推导成业务 Thread。事件分别记录传输 delivery、remote event、entity、实体 revision 和回复关系；原生/派生/legacy/不可用 revision 明示，缺少原生版本不伪造版本顺序。occurred_at 与 received_at 都带时区，允许晚到及平台时钟偏差，不能按接收顺序覆盖旧事实。

SourceCapabilities 强制逐项声明 history_read、incremental_cursor、reconnect_replay、edit_events、delete_events、native_entity_revision。ChannelCapabilities 独立声明 send、idempotent_send、query_delivery、delete_message。每项为 supported/unsupported/unknown，省略字段不能验证通过。Registry 取端口时同时检查注册版本与实现声明；unknown 不当作可用。能力声明不是 Receive/Execution/Notification 授权。

SourceHealth 独立表示连接状态、权限状态、最后事件与 gap。online 可以同时是 permission revoked/insufficient；本卡仅交付契约，不宣称平台在线或真实消息权限通过。

## 端口和唯一写者

`sources/ports.py` 定义 SourceAdapter 的配置 schema、verify、normalize、fetch_context、health，以及 Channel.send/query。TransportEnvelope 的 payload/headers 不进入 repr；VerifiedEnvelope 是受信适配器产生的内存对象，不从来源 JSON 反序列化。DTO 中的 verification_ref/provenance 本身不能授予权限。

`SourceRegistry(engine)` 仅使用现有 Engine 持锁连接的 transaction，不打开数据库连接，不另建 Engine，也不修改 Harness 或 Engine 类。register_instance 追加不可变安装配置版本，安装身份不可改；同一 revision 的不同内容拒绝，跨项目操作拒绝。注册不启动连接、不订阅群，也不授予执行或发送许可。

verify_and_normalize 在 SQL 事务外调用受信 Adapter，重查 verified scope、project、配置 revision、verification_ref 和 provenance，并限制每批 100 个事件。它不替代 ReceivePolicy、不持久保存正文、不 ACK 平台、不路由任务。未来 Gateway 必须先执行 SA03/SA04 的接收范围与持久回执协议。

## 0030 前向迁移和未关联收件箱

新增四个事实表：source_instances、source_instance_versions、source_inbox、source_business_links。前三者保存安装范围、配置版本及未关联最小元数据；最后一个表用外键引用原有 Thread/Trigger/Ticket/Run，不复制业务生命周期。已有表仅增加用于复合外键的唯一约束；不改历史迁移或业务状态枚举。

stage_metadata 接受 SourceMetadata：不保存正文、body_ref、sender、仓库 hint、平台凭据或授权。返回的是内部 UUID，不是平台持久接收凭证。它不创建伪 Issue，也不创建 Thread/Ticket/Run/Action/Outbox；重复元数据版本显式报错。并发传输去重、正文冲突、编辑/撤回裁决、游标、ACK/重放与 gap 恢复留给 SA04，不能把本方法当作完整入站入口。

source_business_links 强制同项目和 Thread→Ticket→Run 关系。当前写入口 link_legacy_receipt 只接受已经由旧 Engine.ingest 持久化的 EventReceipt，逐字段比对真实 trigger payload 与完整 legacy metadata fingerprint，再链接原有 ID。新来源不能借此创建旧触发器。后续新来源关联由 SA04/SA10 在当前身份和策略校验后实现。

安装版本、元数据、关联只追加新版本，UPDATE 被数据库拒绝；保留策略所需受控 DELETE 未被禁止。自动保留/删除、撤权和恢复 tombstone 的实施仍由后续卡交付。本卡元数据不以消息正文留存代替最小审计。

## 旧入口兼容

`domain.py::TriggerInput`、Engine 和 API 源码保持不变。LegacyTriggerAdapter 是 opt-in 工具，不接管旧 Webhook/CLI/Web。wrap 用独立 legacy 安装命名空间和带来源标记的 LegacyTriggerRef，保持旧 repository/actor 规范化与 execution_mode，正文仅保存 SHA-256 引用。旧事件 ID 的特殊字符和超长 Issue/仓库标识在新 opaque key 中用确定性哈希编码，原字段留在兼容上下文，restore 验证完整 wrapper 与正文 hash 后还原原 TriggerInput。

legacy verification_ref 不冒充 GitHub App 签名或飞书认证。新的 verified_source 不能携带 legacy_context；普通来源不能借旧 issue_number 必填结构扩权或补出 issue_number=1。SourceEvent 的 repository_hint 不是仓库 grant。

## 产物与复验

JSON schema 位于 schemas/super-agent-v1/；合成样本位于 fixtures/super-agent-v1/source-v1/samples.json，明确标记 SYNTHETIC_CONTRACT_ONLY。接口清单与 artifact 哈希在 config/source-contract-v1.json。schema 和样本均纳入 scripts/verify.py 的源码指纹，防止仅代码指纹一致却换了输入。

在副本根目录执行 `make migrate`、`make lint`、`make typecheck`、`make verify TASK=SAV1-01`。注册测试使用真实隔离 PostgreSQL：完整 29→30 升级保留旧业务行与全部迁移 checksum，安装内 ID 和版本、无原生话题、晚到与编辑、父事务回滚、外键链、旧 API/Engine/预算约束均有回归。

测试中发现原 test_environment 的两项断言仍停留在 19 个迁移，原 29 迁移基线已复现 2 FAIL/2 PASS 并保留日志。现改为核对实际 migration 名称及 SHA-256，继续严格验证旧 call_limit=1、used_calls=1 和禁止第二次用量，未放宽预算。首轮新代码静态问题及修复记录也保留。

## 未实施边界

SA02 的可信平台身份绑定/控制角色、SA03 的四层策略、SA04 的完整持久接收/回执、SA05/SA06 的真实 SDK/平台接入、SA17 的实际 Channel/Dispatcher 发送均未由本卡实现。没有启动平台监听、模型、Worker、通知或发布；新增付费调用与远端写为零。结构与工程验收不能替代真实来源/编码/发布/长期运行门。
