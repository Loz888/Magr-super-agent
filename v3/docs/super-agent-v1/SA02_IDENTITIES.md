# SAV1-02：可信平台身份与控制角色

契约版本 `source-identity-v1`。状态以 `accept/SAV1-02/accept.json` 为准。本卡提供可信校验端口、身份映射、当前角色检查、审计和已接线的控制台管理 API；真实 GitHub/飞书适配器在 SA05/SA06 验收，当前没有注册生产身份校验器或启动监听。

## 可信身份边界

`SourceIdentities(engine, verifiers=...)` 只接受服务端组合时注册的 `IdentityVerifier`，键为来源种类和 adapter_version。无 verifier 返回 `IDENTITY_VERIFIER_MISSING`，拒绝 legacy 命名空间。校验器必须核对平台签名或安装证明，从已验证信封的真实作者字段提取租户、安装、sender、actor kind 和会话；不得从正文、引用作者、显示名或模型结果推断。`VerifiedSubject` 的 schema 是服务端接口文档，不是公开接收的身份断言格式。

校验在 PG 事务外执行。服务在同一 Engine 事务内重查 SourceInstance 配置、完整 scope、实际 payload SHA-256、时效和非历史标记。回调仅允许过去 300 秒/未来 30 秒偏差，最大 256 KiB；安装内 remote_event_id 不可重复签发。原始 actor ID、id_type、tenant/installation ID 保持大小写、空白及 Unicode 原样，沿用 SA01 的结构化身份键。同名不会合并；actor kind 不在键内，但首次认证后不可变，机器人不能借同 ID 改成人。

身份凭据是仅在受信 Gateway 内存流转的随机 bearer，PG 只保存 SHA-256。凭据绑定身份、项目、来源配置版本、授权 revision、会话、时限与一次平台事件；管理 API 不返回 bearer 或其 hash，也没有 `/authenticate` 或 POST `/evidence`。凭据和身份存在本身不授予 Receive、Execution、Notification 或发布许可。新 Gateway 仍需 SA03/SA04 的接收策略与持久 ACK 协议；这里的严格 proof replay 防护不替代 SA04 事件幂等。

## 管理与跨平台关联

首版复用现有唯一控制台账户、服务端会话、Origin 与 CSRF 检查。服务在持有变更事务后再次核对当前 cookie、账户和有效期，拒绝已撤销但 HTTP 依赖曾查到的缓存会话。旧 X-MGAR-Token 和来源 bearer 都不能管理身份。

管理员核对来源 receipt 后，以 `evidence_id`、`expected_identity_revision`、明确 roles 和 `review_ref` 绑定。默认产生新的内部 principal（`sa_<uuidhex>`），不把 opaque 来源 ID 交给旧 Engine/Harness actor 验证。跨平台必须明确选择已核对的 principal_id；绑定控制台主人须明确 `console_owner=true`。关联到同一 principal 不继承另一会话/安装的角色，也不产生控制台登录权。机器人/服务只能关联 automation principal，并且只能贡献上下文。

一旦身份绑定 principal，本版不支持换绑到另一个 principal；错误关联应立即撤销，并走后续有审计的迁移流程，不能静默改写旧任务 actor。角色可修订，绑定和身份可分别撤销与显式恢复。

## 角色与动作

所有来源角色独立授予，不隐含角色层级。角色绑定在安装内 conversation，任务控制额外限定到已存在的精确 Ticket，引用别人的请求/Ticket 不产生控制权。

| 身份/角色 | 本卡允许的入口 | 附加条件 |
| --- | --- | --- |
| context_provider | provide_context | 当前会话有效绑定；机器人/服务仅允许此项 |
| requester | request_task | 当前会话有效绑定；Admission/Execution 策略由 SA03 决定 |
| task_controller | control_task | 同项目、显式授予的精确 Ticket membership |
| project_administrator | manage_rules | 当前会话有效绑定；规则签署/四层策略由 SA03 提供 |
| 控制台唯一主人 | 身份绑定、角色修订、撤销/恢复和任务控制者授权 | 当前 cookie、Origin/CSRF、事务内重查、乐观 revision |

任何来源角色都不能用 `manage_identities`、`expand_execution`、`approve_publication` 扩权。普通 IM“同意”、引用和模型输出不成为具体 Diff/tree/payload_hash 审批；发布仍使用原 Review Bundle/Action/Outbox 协议。

## 撤权、并发和事务

`with identities.authorized(credential, action, ticket_id) as identity:` 在同一个原 Engine 事务中重查 proof hash、时限、当前来源/身份 revision、撤权、绑定角色、principal kind 和 Ticket membership。受保护的 SQL/Engine 操作必须在此 context 内完成；返回的 IdentityAuthorization 不能缓存成可重用授权，外部调用不得放进此 PG 事务。后续异步工具/发送仍需各自即时检查策略与 epoch。

角色、binding 或 controller 变更由数据库 trigger 推进身份授权 revision，旧 bearer 立即失效；管理员写带 expected_revision，同版本并发更新只有一个获胜。撤权与受保护的 Engine 操作按原连接/锁线性化，撤权提交后新操作被拒绝；已经进入的原子事务先真实收尾。外层事务失败时身份授权审计与业务变化一起回滚。

身份/绑定撤销不会被新消息自动恢复。恢复要求管理员显式操作、当前 revision 及撤权后新验证的匹配 receipt；恢复本身再次使旧 proof 失效。撤销绑定或移除 task_controller 会撤销其 Ticket membership，恢复绑定不会悄悄恢复任务授权。单独恢复全局身份后，未被单独撤销的显式绑定仍需新 proof 才可使用。

## API 与操作

前缀 `/api/v3/source-identities`，所有响应 `Cache-Control: no-store`。下列路径均需要当前控制台会话；写操作另需可信 Origin 与 X-CSRF-Token。

| 方法 | 路径 | 参数要点 |
| --- | --- | --- |
| GET | /evidence、/bindings、/principals、/audit | 仅本项目，最近/最多 100 行，不返回凭据 |
| POST | /bindings | evidence_id、expected_identity_revision、roles、review_ref，可选 principal_id/console_owner |
| PUT | /bindings/{binding_id}/roles | expected_revision、roles、review_ref |
| POST | /bindings/{binding_id}/revoke | expected_revision、review_ref |
| POST | /bindings/{binding_id}/restore | expected_revision、roles、evidence_id、review_ref |
| POST | /identities/{identity_id}/revoke | expected_revision、review_ref |
| POST | /identities/{identity_id}/restore | expected_revision、evidence_id、review_ref |
| POST | /bindings/{binding_id}/controllers | expected_revision、ticket_id、review_ref |
| POST | /bindings/{binding_id}/controllers/{ticket_id}/revoke | expected_revision、review_ref |

管理流程：受信平台验证器产生 receipt → 主人在 evidence 列表核对真实来源/作者/会话 → 明确选择角色及跨平台关联目标 → 读取写响应的新 revision → 后续动作使用新事件产生的内部 proof。绑定本身推进 revision，因此绑定前的 proof 不能直接执行动作。当前未接入真实验证器，不能通过手工伪造 JSON 建立真实来源身份。

## 持久化、兼容与验证

0031 仅增加 source_principals、source_identities、source_identity_evidence、source_role_bindings、source_task_controllers、source_identity_audit 六个身份/授权事实表，FK 指向原 source_instances、web_accounts、Ticket/Thread。只使用原 Engine 的持锁 PG 连接，不新增业务状态机、独立写连接或 Engine 方法。原 TriggerInput、旧 API 行为、模型、预算和 Harness 不变；api.py 只增加注册 import 与调用两行，旧迁移 0001–0030 不改。

审计只保存 ID、固定 reason code、review_ref 和少量角色/revision 信息，不保存正文、显示名、原始信封或 bearer。成功管理与受保护业务操作同事务；DomainError 拒绝在该事务回滚后通过原 Engine 记录最小拒绝审计。若调用者另开外层 Engine 事务，其最终回滚也会回滚嵌套拒绝记录；本卡没有另开数据库写者保证跨外层回滚的审计。HTTP 框架先行拒绝（无 cookie/Origin/CSRF/结构错误）沿用旧入口行为，不承诺落入身份审计。

在副本根目录执行 `make migrate`、`make lint`、`make typecheck`、`make verify TASK=SAV1-02`。注册目标覆盖新身份负例/管理 API/并发撤权/回滚/30→31 升级，以及 SA01、旧账户/API/Engine/预算回归。身份测试使用真实一次性 PostgreSQL；签名测试是真实 HMAC 校验合成信封，不是真实 GitHub/飞书平台成绩。三份发布 schema 纳入源码指纹，manifest 见 config/source-identity-v1.json。

0031 和新 API 路径的共享所有权已由 Trae 在固定提交 `0ca76063ab91177cd7255c475435d3d6b00c726c` §13 实际确认（2026-11-10T14:50:28Z），非排队消息。没有导入/编辑 Harness 活动文件，没有预占 0032。共享 Engine/API/预算或下一迁移仍需另行对齐。

## 未启用与限制

SA03 四层策略、SA04 持久来源接收/游标/ACK、SA05/06 真实适配器、SA17 发送与 SA19 管理页面不由本卡实现。当前没有生产 verifier、平台监听、模型调用、Worker、通知或发布；所有 live 门和长期运行门 NOT_RUN，付费调用/外发为 0。最小身份审计的保留/删除及恢复 tombstone 仍需 SA04/SA21 生命周期方案，不将本卡证明扩展到完整隐私安全门。
