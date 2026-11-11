# SAV1-06：飞书机器人来源适配

契约为 feishu-bot-v1。本卡实施范围是本机受控工程：官方 SDK 接线、来源设置、
当前权限复查、持久入站及有界只读历史端口。固定源码15591aea214e7a83f3bf5ad112bda85b4294664e完整注册744项及lint/typecheck已通过；证据与限制见
SA06_ACCEPTANCE_20261111.md及accept/SAV1-06/accept.json。独立复验仍PENDING，
不宣告整卡独立放行。真实飞书渠道、真实 GitHub、模型、编码、通知、发布及长期运行门保持
NOT_RUN。没有登记实际凭据、启动真实监听或向平台发送消息。

SA02 身份与角色、SA04 持久入站是本卡依赖；原 SA03 四层策略继续生效。只接收
来源数据不会授予执行、模型调用或通知许可。迁移 0035_feishu_source.sql 仅追加
飞书配置、版本、健康和最小传输审计；0001–0034 不应被改写。共享迁移及接口
所有权的实际确认在本卡 start evidence 的 ownership-confirmation.txt 中。

## 1. 唯一权威和工程能力

所有业务输入仍由原 SourceGateway 提交给持有原 PostgreSQL 连接和 advisory lock
的唯一 Engine。适配器、SDK、Broker 和控制台不另建业务状态机，不创建伪 Issue，
不直接写 Task、Ticket、Run、费用或完成状态。平台 I/O 在 SQL 事务之外；最终
入站提交重新核验当前 Source、ownership generation、策略、身份与角色。

| 项目 | 本卡实现 | 可靠性或权限边界 |
| --- | --- | --- |
| 官方长连接 SDK | 锁定 lark-oapi==1.7.5；实际调用官方帧解析、EventDispatcherHandler 和 ACK 序列化路径 | 本轮使用受控 Socket，不代表真实握手、平台重推或租户 scope 已验收 |
| 消息事件 | im.message.receive_v1；保留 App、tenant、chat、message、sender、原生回复关系与时间 | 人类身份来自受信 sender_id.open_id，不由显示名、正文 @、引用或 SDK typed object 产生授权 |
| 撤回事件 | im.message.recalled_v1 转为已验证 App service 的 tombstone | SDK 撤回结构没有可验证的人类 operator；不冒用原作者或管理员 |
| 群与用户选择 | 精确 chat_ids、sender_open_ids、context_bot_open_ids，区分人类和机器人 | 私聊、未选群和自身机器人不形成可用消息输入；opaque ID 保持原始大小写 |
| @ 与话题 | 验证结构化 mentions 中本机器人的 open_id；可跟进显式 followup_topics | 正文出现 @ 或他人名字不算 mention；话题取 thread_id 或 root_id，parent_id 单独保留 |
| 文本与富文本 | text、post 保存有界规范化正文，仍须当前 Receive 允许留存 | 没有附件下载、URL 抓取、模型或 Recall 接线；保留正文不等于可以向外披露 |
| 当前历史快照 | 受信服务端 FeishuHistory，经 SourceGateway.history 提交 | 仅明确获准群；每页最多 50 条，最多 100 页；所有历史默认 context_only |
| 未支持的历史正文类型 | 保留真实类型、作者、资源关系与内容 hash，提交不含正文的版本事实并记录 gap | 不伪装成 text，不解析或下载附件，不跳过已知变化导致旧正文继续可读 |
| 编辑与排序 | 无原生单调修订序号；使用内容及真实资源事实构成 derived revision | 不按到达时间或 update_time 猜版本先后；不同无序版本沿用 SA04 sticky ambiguity |
| 健康观察 | connection、permissions、mode、reason_code、最近事件及 gap 分开记录 | online 不表示 scopes granted；受控证据始终标 controlled，实际渠道门仍为 NOT_RUN |
| 发送及结果回帖 | 未实现发送 Channel，Broker 没有发送端点 | 无通知许可、无自动回复、无发布；后续 SA17 等卡另行实现和验收 |

通用 Source 能力声明中，history_read 为 supported；incremental_cursor、
reconnect_replay、edit_events、native_entity_revision 为 unsupported。这里的
history_read 表示受控端口可以取得当前资源快照，不能解释为完整消息变更日志。

## 2. 平台权限与本地许可矩阵

requested_scopes 是管理员审阅的平台权限需求清单，不是平台返回的已授予证明；
scopes_review_ref 记录审阅依据。飞书后台权限、机器人是否在群、来源设置、
SA02 身份角色和当前 Receive 都必须分别满足。仅安装应用或配置 enabled=true
不会激活运行时。

| 用途 | 平台要求或请求权限 | 本地额外条件 | 本轮实际平台结论 |
| --- | --- | --- | --- |
| 群内 @ 机器人事件 | im:message.group_at_msg:readonly，订阅 im.message.receive_v1 | 已选群、已选 sender、真实结构化 @ 或已选回复话题、当前 Receive | NOT_RUN |
| 接收所选群中的全部所选用户消息 | im:message.group_msg；本卡配置仍要求基线 @ scope | receive_mode=all_selected，独立审阅敏感权限；不能由正文请求自行切换 | NOT_RUN |
| 以应用身份读取群历史 | im:message:readonly 及 im:message.group_msg；机器人须在目标群 | history_enabled=true、当前 allow_history_context、相同群/话题及有效 context_provider | NOT_RUN |
| 只读机器人身份探测 | 显式调用 GET /open-apis/bot/v3/info | 当前 health activation；返回 bot open_id 与 app_id 必须匹配 | 仅受控响应；scopes unknown |
| 撤回处理 | 订阅 im.message.recalled_v1，具体平台订阅资格须实际核验 | 事件已选择、安装/租户/群可信；只提交 service tombstone | NOT_RUN |
| 私聊、跨租户、未选群 | 平台即使可投递也不增加本地许可 | 本卡拒绝作为可用正文输入或历史读取目标 | 无真实调用 |
| 发送、附件下载、仓库、模型或工具 | 不属于本卡可用端点 | 本地未授予相应执行/通知许可 | NOT_RUN |

官方[获取会话历史消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list)
明确区分应用身份和用户身份权限。当前 Broker 使用应用身份的 tenant_access_token，
不使用用户 token。本项目仅配置群容器，不开放平台接口本身支持的私聊读取能力。
平台要求的 group_msg 不等于本地 Receive 必须采用 all_selected：可以对历史单独
审阅该 scope，同时保持实时消息只处理 @ 或选定话题。

本地设置保存检查明确的 scope 清单；该检查不能证明平台已开通权限。机器人身份
探测也不能列举或证明全部 scopes。probe_permissions 返回 permissions=unknown、
scopes_verified=false；响应没有 tenant_key 时 tenant_identity_verified=false，
不会把设置中的 tenant 当作远端已返回的证明。显式的 tenant、App 或 bot 不匹配
拒绝并 fence 当前来源。

## 3. 服务端配置引导

本节描述受信服务端如何组装本地工程，不是启动真实平台的操作授权。当前没有
完整生产凭据登记界面或 SA19 可视化向导。默认 create_app(feishu_runtime=None)，
默认没有 FeishuActivation；保存来源配置不会创建 Runtime、启动线程或建立连接。

1. 使用锁定环境与显式迁移，保留已有用户修改。注册 SourceInstance：source_kind
   为 feishu，installation_id 为 App ID，tenant_id 为平台 tenant_key，adapter_version
   为 feishu-bot-v1，能力取适配器固定声明。credential_ref 只保存服务端凭据引用，
   不保存 Secret 或 token。首版按明确项目、安装和租户隔离。
2. 通过当前控制台主人 cookie 保存非密钥设置。PUT /api/v3/feishu-sources/{source_id}
   要求 expected_revision CAS 和 review_ref；写操作继续使用原 Origin/CSRF 保护，
   响应 no-store。GET 同一路径仅读取已保存设置、审计和近期健康，不触发网络探测。
3. 明确配置 App、tenant、bot open_id，群、发送者、上下文机器人、事件、接收模式和
   话题。App/tenant/bot 三元组在已保存来源中不可变，更换安装须注册新来源。
   自身 App/bot 不能加入输入白名单，人类和机器人名单不能重叠。
4. 使用 SA02 的受信平台证据，由管理员核对身份并绑定适当角色；正文、显示名、
   模型输出和历史快照不建立新身份。根据当前 Source revision 签署四层策略，
   再配置 SA04 owner=gateway、精确 policy revision 和 ownership generation。
   策略群、actor、事件不能超过飞书设置上界。未完成这些步骤时 Runtime 失败关闭。
5. 真实密钥由服务端自己的安全配置系统解析后注入 FeishuSecrets，包括 app_secret、
   verification_token、encrypt_key。本卡不从请求正文、普通环境开关或项目文件
   自动加载密钥，也没有将凭据写入 API、worker、模型上下文、SQL 或日志的功能。
   本卡提供注入端口，不声称已经交付生产 Vault、密钥轮转服务或秘密分发系统。
6. 受信服务端显式提供 FeishuActivation，绑定 source_id、当前 config_revision、
   ownership generation、UTC 有效期、允许操作集合和 review_ref。本轮只使用
   controlled；Broker 必须注入 httpx.MockTransport。没有 Runtime/activation、
   过期、revision 或 generation 不符都会拒绝后续操作。
7. 只有显式将 Runtime 注入同一个 Engine 的 create_app 后，可选 Webhook 才能
   使用该服务端对象。长连接也须由受信服务端单独构造；本卡不自动运行 start。
   任何未来真实连接都还需要独立平台范围授权、live activation 和 Socket 的
   当前 live_gate，不能把本地 controlled 结果换成 live PASS。

以下是只含非秘密字段的配置形状；默认保留停用，不会建立连接：

~~~json
{
  "expected_revision": 0,
  "review_ref": "review-local-sa06",
  "settings": {
    "schema_version": "feishu-bot-v1",
    "app_id": "App-Reviewed",
    "tenant_key": "Tenant-Reviewed",
    "bot_open_id": "Bot-Reviewed",
    "chat_ids": ["Chat-Reviewed"],
    "sender_open_ids": ["Sender-Reviewed"],
    "context_bot_open_ids": [],
    "events": ["im.message.receive_v1", "im.message.recalled_v1"],
    "receive_mode": "mentions_or_topics",
    "followup_topics": [],
    "requested_scopes": ["im:message.group_at_msg:readonly"],
    "scopes_review_ref": "review-platform-scope-list",
    "history_enabled": false,
    "enabled": false
  }
}
~~~

每次配置保存追加原 SourceInstance revision，保存配置版本与签署审计，并删除旧
健康缓存。旧正文、策略、Runtime 和 activation 不能自动升级。重新确认来源、
角色、策略和 ownership 后才可建立当前 Runtime；健康恢复不是自动恢复工作许可。

## 4. 官方 SDK、ACK 和断线边界

SDK 版本和实际 Client/Dispatcher 源文件 SHA-256 由 sdk_facts 检查；单纯安装
同名包或升级版本不会绕过检查。参考[官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)
与[长连接事件接收说明](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)。
官方仓库当前分支可能继续变化，本卡 ACK 结论绑定安装的 1.7.5 实际源码及本卡证据。

Socket 的受信连接 epoch 和私有证明封装来自 Runtime，不接受 HTTP header 自称
SDK 已验证。官方 typed event 仅用于 SDK dispatch，实际身份和授权重新读取未经
替换的原始有界事件字节。接收路径为：

~~~text
官方原生 frame
  -> 官方 SDK 解析/事件分发
  -> Runtime 验证当前连接证明与来源范围
  -> SourceGateway
  -> 唯一 Engine/PG 持久提交
  -> handler 返回 committed receipt
  -> 官方 SDK 序列化并发送成功 ACK
~~~

验证或 PG 提交失败时 handler 不正常成功返回，SDK 可产生失败响应，不能把“有响应”
算作成功 ACK。持久提交后成功 ACK 丢失时，重投由 SA04 canonical 去重复用输入；
不能依赖客户端内存标志声称事件已持久化。来源永久过滤也须先保存有界、无正文的
传输拒绝凭证才成功返回，它不是第二个业务收件箱。传输 ID 对应不同 payload
明确冲突；账本容量不足时不伪造成功。

SDK 接入使用串行接收和有界等待，避免原生无限创建处理任务。当前限制为消息
256 KiB、最多 16 分片、最多 32 待处理/未完消息、分片总缓冲 1 MiB、TTL 5 秒。
这些分片只在内存中组装，不是持久重试队列。过期或断线清除残片、失效连接证明，
记录真实 gap；旧连接的晚到输入不能继承新连接的 epoch。ACK 丢失不改变已提交
事实。写 ACK 捕获原连接与 epoch，取得发送锁后重新检查；旧 ACK 排队跨重连的原生协议负例已纳入本卡回归，仍不概括为所有竞态已独立验证。

自动重连在当前组装中关闭；显式受信重连也最多三次，服务器配置不能改成无限
重试。心跳与重连参数只表示协议观察。重连将连接状态改为在线不等于丢失内容已经
重放；重连时保留缺口/不可重放记录。官方说明的超时重推是平台行为，不能解释为
无限重放、永久离线队列或端到端 exactly-once；真实超时、保留期和平台重推仍待
另行渠道验收。本轮 SDK PG 专项重建 Engine 连接不冒充独立进程 SIGKILL 或真实
网络断开；整卡故障检查范围以最终注册证据逐项列出。

可选 POST /api/v3/webhooks/feishu 复用当前 Runtime；默认没有 Runtime 返回
FEISHU_RUNTIME_DISABLED。Webhook 校验签名 header、时间/nonce、App、tenant、
verification token 和 schema；加密事件使用官方 SDK 解密。重复认证 header、
重复 JSON key、假签名和过期回调拒绝。成功消息响应在持久提交后产生。经验证的
challenge 是协议握手，不作为消息输入。Socket 私有证明不能由 Webhook 伪造。

SDK 日志入口丢弃原始 payload、连接 URL 和任意异常文本参数，避免默认 SDK 调试
日志泄露正文或凭据。过滤、错误、权限丢失只记录固定代码和最小有界事实。

## 5. 只读 Broker、历史和隐私失效

Broker 固定 https://open.feishu.cn，禁止环境代理与重定向，使用十秒超时和单响应
2 MiB 上限；不跟随消息、附件、正文或远端错误中的 URL。服务端只允许以下用途：

| 方法与固定路径 | 用途 | 对外暴露情况 |
| --- | --- | --- |
| POST /open-apis/auth/v3/tenant_access_token/internal | 为显式 history 或 health 操作取得短期 tenant token | 私有 Broker 调用栈；没有公开 mint/token 端点或持久 token cache |
| GET /open-apis/im/v1/messages | 已授权群的当前历史快照 | 受信 FeishuHistory 端口，经唯一 Gateway |
| GET /open-apis/bot/v3/info | 显式机器人身份探测 | 服务端 probe_permissions；设置 GET 不调用它 |

本卡锁定官方 SDK 已包含的 internal token 协议。该 tenant token 继承应用在租户的
实际平台权限；不能声称它在平台侧已被衰减为只读 scope token。只读约束由 Broker
固定端点、当前授权和不向其他进程释放 token 落实。较新官方 token 文档的接口、
域名或可选 scope 不会被自动启用；真实平台兼容性须另验，不能静默切换获取方式。

历史读取前、mint 前后、真正 GET 前和返回数据前，重新检查 activation、配置、
ownership、Source revision、有效策略、Receive 历史许可、群/话题以及当前有效身份
与 context_provider。mint 中撤权不再 GET，GET 中撤权不释放结果；已经发出的
读取无法倒流撤销，但不被当作新许可。没有授权不会请求 token 或消息；本卡没有
Worker/模型消费历史的自动入口。

每页最多 50 条；游标使用有界的页码和编码平台 page_token，最多 100 页，不接受
任意 URL 或重复游标。不支持的来源/话题、越界页、错误 provenance、平台不可用、
达到页数上限仍 has_more，以及无法重放都留下 gap，不伪称补齐。页面跨群/话题在
业务提交前拒绝；页面提交不是跨平台原子事务，已提交部分可按真实凭证幂等重交。

历史 API 只证明消息当前作者与当前资源快照，不证明最后编辑者。原作者不能被
伪装为“刚执行编辑的操作者”；撤回快照只形成 App service 的 tombstone。没有
原生修订序号时，时间与内容 hash 不能取代排序证明。update_time=0/空值按未编辑
观察处理，等价实时事件与历史快照在任意顺序下按 canonical 版本收敛；真实内容、
身份、作用域或权限矛盾不被当作等价观察吞掉。

已授权但过大的替代正文、未选作者和不支持的历史消息类型，仍须保留受信的最小
变更事实以失效旧正文；不能为了过滤新内容而跳过整行。新正文不保留，不建立
未选作者身份，不把附件元数据装作可读正文。不同无序版本保持 ambiguous；删除
tombstone 不因晚到 create 或历史快照复活。历史每条输入由 Engine 再检查当前
Receive，并按 context_only 处理，不批量开工。机器人上下文也不提供任务请求、
控制、扩权或审批权。

已识别的 HTTP 401/403、HTTP 200/400 响应内明确无效 token/缺 scope 的错误码、
429、响应丢失与格式/大小错误分别记录固定状态。历史 API 的 230027 明确记为
insufficient 并 fence 当前来源；230073 明确记为 FEISHU_THREAD_INVISIBLE 和
permission gap。当前没有已验的话题级旧正文撤权原语，因此对后者保守 fence 整个
本地来源，使此前正文失效，等待管理员重新确认；不把它伪称为整个平台 App 已被
撤销。未知 HTTP 400 业务码保留不可用/unknown，不推断成功或撤权；HTTP 400 即使
body 声称 code=0 也不能释放数据。权限丢失推进现有来源版本，旧正文、策略和 Runtime 被 fence；已在途
结果不能复用旧许可。429 记录有界 Retry-After 等待，不自动换 token 重试。
未知远端错误不被猜成 permission granted，不保存远端错误正文或伪造读取成功。

## 6. 工程复现、证据和未放行范围

在本副本 v3 目录使用锁定环境运行 make lint、make typecheck、make verify
TASK=SAV1-06。实际注册入口、退出码、JUnit、source_commit/source_sha256 与
最终固定提交由主任务验收记录给出；本文不预填未运行的整卡统计或 PASS。

本卡 start evidence 位于 evidence/super-agent-v1/SAV1-06/20261111T064222Z-start/。
阅读 UTC/SHA、依赖与所有权记录在该目录。SDK 的 agent-sdk/agent-result.json
和 Broker 的 agent-broker/summary.json 是已运行子任务的材料，不替代最终固定
源码的完整注册检查。

Broker 曾保留四项真实历史 unsupported 边界 FAIL，修正后针对四项失败和两项新
边界重跑六项通过。SDK 子任务也保留初次 fixture 失败、后续修正与重跑记录。
不能把多个源码状态的分轮结果相加冒充一个候选的完整全绿。上游 SDK 的两项
DeprecationWarning 保留在原日志，未以隐藏警告修改其运行结论。

另有官方 HTTP 400 业务拒绝回归：修复前两项权限分类 FAIL、两项未知/矛盾响应
负例 PASS；修正分类后，首次相关检查中两项测试误查 gap 列的失败也原样保留。
改为读取既有 declaration JSON 字段后，十五项相关检查全部通过。原始红灯、
中间轮次、最终日志和 JUnit 见 agent-broker/http400-*；摘要为
agent-broker/summary-http400.json。该结果仍不是整卡固定候选的独立放行。

真实飞书安装/授权、TLS/WebSocket 握手、平台投递/重推、实际历史可见性、真实
权限撤回和 token 生命周期均为 NOT_RUN。真实 GitHub、付费模型、真实编码、
外发、发布、完整系统安全与长期运行也没有因本卡本地工程而获准。本卡完成后
停在 SA06 等待独立复验，不能自行进入 SA07。

## 7. 官方参考与契约

以下为 primary sources；网页会更新，工程判断还绑定本卡保存的 SDK 版本与源码
指纹。查询文档没有调用真实平台 API。

- [接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)
  与[Python SDK 事件处理](https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events)。
- [使用长连接接收事件](https://open.feishu.cn/document/ukTMukTMukTM/uYDNxYjL2QTM24iN0EjN/event-subscription-configure-)。
- [获取会话历史消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list)
  与[应用权限申请](https://open.feishu.cn/document/ukTMukTMukTM/uQjN3QjL0YzN04CN2cDN)。
- [官方 Python SDK 仓库](https://github.com/larksuite/oapi-sdk-python)；本地核对
  lark_oapi/ws/client.py、event/dispatcher_handler.py、api/im/v1/model/ 下的
  ListMessageRequest、Message、Sender、Mention，以及 channel/bot_identity.py。
- [internal tenant token 文档入口](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)
  与[当前 tenant token 文档](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/tenant_access_token)；
  后者的较新协议不表示本卡已实现或验证它。
- [机器人概述](https://open.feishu.cn/document/client-docs/bot-v3/bot-overview?from=home)
  与[通用服务端错误码](https://open.feishu.cn/document/server-docs/api-call-guide/generic-error-code)。
- 本仓库 [SA02 身份契约](SA02_IDENTITIES.md)、[SA04 入站契约](SA04_INGRESS.md)、
  [架构](ARCHITECTURE.md)、[验收协议](ACCEPTANCE.md)及适用
  [身份 ADR](adr/SAV1-02-identity-authority.md)、
  [持久入站 ADR](adr/SAV1-04-durable-source-ingress.md)。

## 8. 历史身份表示的保守限制

官方历史 Mention 是扁平 id/id_type，实时事件则使用嵌套 UserId；Broker 保留实际
id_type/tenant 并转换结构。仅当平台提供相同 OpenID、正文和资源事实时，受控录制
中的双向 canonical 去重已覆盖。只有 user_id/union_id 时不猜测本机器人的 OpenID，
缺 tenant 拒绝；缺 mentions 或不同正文表示也不宣称等价。相应差异可能留下保守
ambiguous/gap，真实平台实际返回表示仍 NOT_RUN。derived revision 纳入是否确实
@本机器人，未纳入全部其他被提及者身份集合；不宣称检测了所有 mention 身份差异。

## 9. 撤权后的健康诊断

来源被fence后仍显示当前配置/运行模式下、观察年龄[-30s,5min)内已验证的
insufficient/revoked及准确reason；runtime_current=false明确这不是恢复授权。
过期/未来或旧配置/模式记录不继承，失效Runtime的旧granted/online不会复活。
初候选729注册PASS后新增登录API边界发现遗漏，原始1FAIL及正式3FAIL保留；
后续修复和完整新候选结果单列，不能用原729全绿替代该问题验证。

## 10. 独立失败后的修复边界

当前修复候选与准确结果以SA06_REPAIR_20261111.md及accept/SAV1-06/repair-20261111.json为准，
本卡仍独立NOT_PASSED。native_thread_id不再回退到root_id；Root/parent仅回复引用。
只有原Engine持久提交的可信真实thread_id能建立带配置/receipt/metadata TTL的hash证明，
该事实不授予权限。每次mint/GET前重验当前证明及Receive；无证明网络前拒绝+gap。
有证明使用thread容器，chat列表耗尽不包含话题回复、complete=false；不尝试扩大查询。
游标由服务器MAC绑定scope/config，重启失效，不把裸page_token当可跨范围许可。

SDK清理只更新连接观察，不能覆盖或续命同配置/模式新鲜insufficient/revoked的原因及
时间；audit单独保留新的连接观察。解码/大小/形状错误均固定诊断及health，历史记gap，
不泄露原响应或误推撤权。原话题探针缺来源证明，允许的网络前拒绝仍使其exit1；
此限制原样保留，不修改原探针/评分器放行。完整新注册795PASS也不替代独立验收。

## SA06-I04 修复候选（仍未独立放行）

候选 `dfab5c3f449653bd4126309018559ea10d9bd78a`，指纹 `b26d96a76edfa81c3f7ea1df720e304c77a8429838ae8397c5e79ca300cb063d`。可信历史坏正文仍固定拒绝并记录健康/gap；同页资源元数据及范围全部通过后，仅经原 Engine 事务提交已知资源失效，零新业务输入，保留较新负面权限诊断及原时间。新增30项正式回归；最终固定候选完整注册837项、原35项用例修复方回放、lint/typecheck231files及33交付哈希通过。原独立33PASS/2FAIL及本轮红灯原样保留。SA06仍NOT_PASSED，SA07未启动，真实平台/付费/外发/发布NOT_RUN。详见[本轮报告](accept/SAV1-06/i04-repair-20261111.json)。

## SA06-I04 独立关闭与用户工程放行

用户确认固定dfab5c3原两项失败独立定向复验2PASS/exit0，I04可关闭并推进SA07。原健康/gap/拒绝继续生效，旧正文与密文失效，零新增业务输入。这里只记录该定向独立结论，不将此前33项、全量837项或静态检查改写为本次独立重跑。SA06本地工程范围DONE；完整系统安全、真实平台/付费/外发/发布/长期门仍NOT_RUN。[关闭记录](accept/SAV1-06/i04-independent-closure-20261111.json)。
