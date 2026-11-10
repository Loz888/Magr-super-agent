# SAV1-03：四层策略、版本与撤权

契约 `source-policy-v1`。整卡状态见 `accept/SAV1-03/accept.json`。本卡实现四层独立定义、确定性验证/评估、控制台签署/修订/撤权、只读影响预览及现有 Engine 上的来源授权 fencing；不启动来源、模型、Worker 或外发。实际平台/编码/发布/长期运行门仍 NOT_RUN。

## 四层和模式

| 层 | 明确边界 | 不隐含的授权 |
| --- | --- | --- |
| ReceivePolicy | 当前 SourceScope/安装配置 revision、群/话题、ActorRef、事件类型；保留正文/TTL、模型输入、历史上下文分别开关 | Admission、Execution、回复、发布 |
| AdmissionPolicy | 显式请求、精确 Ticket 跟进、或管理员签署的 rule_ref/rule_sha256 | 仓库/路径/工具执行与发送 |
| ExecutionGrant | 仓库、固定 base SHA、规范相对路径前缀、逐工具 capability、明确网络 hosts、策略时限 | 额外仓库、未列工具、付费模型、通知或发布 |
| NotificationGrant | 明确渠道/会话/目标/可见性，内容类别、披露类别、实际 body hash/bytes、每小时上限与安静时间 | 任意群发、私密转公开、Draft PR 审批 |

所有身份 ID 保持 SA01/02 的大小写、空白和 Unicode 原样。策略 selector 的 actor 是管理员指定的允许对象，实际评估仍取可信 bearer 对应的真实作者和会话；正文/显示名/模型不能提供身份或权限。Topic 限制使用 native_thread_id，没有原生话题不臆造。相同正文在不同 actor/租户/安装/会话/事件下不继承授权。

`disabled` 不创建该策略的新工作或自动回复，但 Receive 的开关独立；`shadow` 对显式请求也只返回 would_allow，不能绑定执行、建 Worker 或发送。`auto_investigate` 只允许列出的只读能力；`auto_repair` 允许在明确范围内评估 edit_files/run_tests，实际 Worker 仍受 Harness 能力门、隔离、旧 Mandate 和未来 SA13 接线约束。没有模式自动批准发布。

请求者必须同时有独立接收/上下文角色与 requester，跟进必须有当前 task_controller 及精确 Ticket membership。team_rule 可将明确列出的自动化上下文信号作为候选，授权来自管理员签署的规则/hash，不来自机器人文字；机器人仍不能修改角色、撤权、扩权或审批。

## 版本、签署和管理 API

策略 ID 与 SourceInstance 一经创建不可变。创建 `expected_revision=0`，修订/撤权带当前 revision；Engine 同一事务执行 CAS，只有一个同版本写者获胜。修订追加不可变定义/hash/签署人/UTC/review_ref，再推进 current_revision；撤权也追加新版本，不能复活该 policy ID。错误策略应撤销并签署新策略，不把旧任务静默升级到新版本。

签署记录指现有唯一控制台主人以当前 cookie、可信 Origin/CSRF 进行的明确保存，PG 保存定义 SHA-256、signed_by、signed_at 和 review_ref；它不是公钥数字签名，也不把来源文本中的签名人字段当授权。HTTP 依赖之后，服务在变更事务内再次核对当前 cookie，缓存的已注销会话不能写入。旧 X-MGAR-Token、来源 bearer、project_administrator 的普通聊天不能代替此控制台签署。

| 方法 | `/api/v3/source-policies` 下的路径 | 行为 |
| --- | --- | --- |
| PUT | /{policy_id} | expected_revision、definition、review_ref；创建或追加签署版本 |
| GET | /{policy_id} | 当前定义、revision、hash 与签署人/UTC |
| GET | /{policy_id}/history | 不可变版本和撤权历史 |
| POST | /{policy_id}/preview | 候选 definition、expected_revision、actor/thread/facts；只读比较，不保存、不授予权限 |
| POST | /{policy_id}/revoke | expected_revision、review_ref；追加撤权并使旧任务失效 |

全部路由需要当前主人会话，写/预览 POST 还要 Origin 与 X-CSRF-Token；响应 no-store。没有接收 source bearer、绑定 Run、执行工具、触发模型或发送消息的公开端点。

预览返回定义 hash、四层决定/would_allow、关联 Run/将失效数量、排队/在途工具、未过期 Worker 租约、待发/在途或 unknown Action 数。预览不写任何策略/审计/Run/Outbox/预算记录；这些候选数据不会成为真实执行凭证，保存仍需 CAS。通知预览明确还需具体 payload 验证，语义模型评估始终 NOT_RUN。

## 受信运行端口与兼容范围

`SourcePolicies.evaluate(credential, policy_id, revision, layer, facts)` 先以 SA02 验证 bearer/current identity/会话角色，再重查策略当前版本、来源配置、有效期和作用域。facts 只供受信 Gateway 提交经校验的候选元数据；不是让模型或公开 JSON 自称 explicit_request、公开内容或目标 base SHA。SA04/05/06 尚未接入真实事件，这些函数不替代平台验签、接收范围和持久 ACK。

`bind_run(...)` 是内部限制桥：仅给已经由旧 Engine 获准创建、尚未开始的 fixture 调查 Run 增加策略约束；不创建任务，不升级旧 actor/permit，不修改旧 Mandate。固定到原调查的 `fixtures/orders/service.py`、仓库及声明的合成 base；它不证明真实 Git 快照或 Coding Worker 已接线。真实/live、旧 repair 图和静默恢复不从该桥开放；旧 read_only Mandate 不能被 auto_repair 升级。真实来源任务创建与 Coding Worker 接线仍在 SA04/SA13。

内部 Run 关联保存当前 policy revision、真实身份/角色 binding revision、原证据和候选 facts；复合 FK 强制 Run→Ticket→Thread→project 与 policy/identity 同项目。关联不可删除、换绑或重新激活。后续工具每次检查当前权限，不缓存 LayerDecision/IdentityAuthorization；长期任务的授权时限来自签署策略，起始短期 bearer 仅用于建关联时验身份，不能拿过期 bearer发新指令。

旧未关联 Run 在新增 guard 立即返回，旧 Webhook/CLI/Web、只读 R6、模型 profile、审批和费用算法保持原样。新增管理 API 只在 api.py 增加 import/register。`agents/tools.py` 只增加一次当前 source-policy 的逐 capability 检查，放在原 charge 前，拒绝不会消耗工具预算；原 SQL charge 表达式未改。

## 撤权与在途结果

0032 的授权事实直接关联原 Run，没有第二个业务主状态机。策略修订/撤权、SA02 身份/角色 revision 变化及来源配置版本变更，在原 Engine 同一 PG 事务中触发 fencing：

- 关联授权标记不可逆失效；未完成 Run 暂停并推进原 execution_epoch。
- 原 Worker heartbeat lease 过期，旧 epoch 不能续租/开始工具；工具 bearer 删除、queued read job 取消，目标写 grant 停用。
- proposed/approved/ready Action 和 ready Outbox 取消；dispatching/outcome_unknown 保持真实状态、原回执 token 与对账能力。
- 已在途 read job 的 token 只可提交一次真实结果，结果存原 artifact/read_job 并记 late receipt；不进 Mailbox，不改任务成功或复活 Worker。
- 晚到外发成功或只读对账更新原 Action/Outbox 的真实回执，但失效 Run 保持暂停。未知不能重发；费用 reserved/unknown 不退款，原 settle_call 仍能记录真实后续结算。

撤权与新操作经原 Engine 锁/事务线性化。claim 是现有派发边界：已成功领取的调用视为在途；本卡不声称能撤回已经发出的远端请求或停止未来 Harness OS 进程。外部调用与 artifact 写仍在 PG 事务外。实际进程/租约安全点须由 SA12–14/Harness 独立验收。

每个操作即时检查策略有效期，即使未清扫也不能用过期授权执行。受信维护端口 `expire_authorities()` 可幂等落地过期 Run/租约/待发 fencing；本卡没有启用周期服务，常驻清扫由 SA20 配置。成功管理、版本与 fencing 一起提交/回滚；拒绝最小审计通过原 Engine 追加，调用者外层事务回滚会连同嵌套拒绝记录回滚，不另开写连接。HTTP 预鉴权/结构错误沿用旧 API 处理，不声称全部写入策略审计。

## 通知与预算边界

`authorize_notification` 仅给具体旧 issue_comment Action 添加独立策略事实，绑定其完整不可变 payload_hash、实际 body SHA-256/长度、显式 destination/target/visibility、内容类别和披露类别。私密类别即使列在总允许类别中，也不能送到 public destination；目标渠道种类必须匹配 scope。仍需旧 Engine 的精确 Action 审批/Outbox/当前目标写 grant，策略事实本身不创建 Outbox。

安静时间使用 IANA 时区，跨午夜以区间开始日为准，结束边界不含，DST 重复小时两个实际时刻均受限。授权时预留每小时额度；派发前再次检查当前安静时间与实际 dispatching 事件窗口，延迟到下一窗口的旧队列不能造成超限突发。额度按同 policy ID 跨 Run/版本统计，unknown 不被当作未发送而重试。暂时被安静时间/频率拦截的旧 Action 按原 claim 协议过期/取消，不自动重新排队；更完整的聚合/延迟 Delivery 在 SA17。

NotificationGrant 不给 Draft PR 发布许可。内容类别/公开性元数据必须由受信服务产生，本卡只校验明确声明、目标和精确 body/hash；完整语义披露判定、渠道发送/回执、Recall/保留/删除须在 SA15/17/21 验收，不能由类型标签证明隐私安全。

BudgetBoundary 冻结项目总额、Router 子额、时区与 accounting_ref，Router 子额不能大于总额。它不是新的付费授权或第二账本。SA07 的共享预占/配额尚未提供，`model_call` 在此版本明确拒绝（SOURCE_SHARED_BUDGET_NOT_AVAILABLE），语义评估 NOT_RUN；正数计划额也不能开模型。本卡不修改 reserve_call/settle_call 或其他财务算法，不退款未知费用。零付费 fixture 测试使用受控账本行证明晚到结算，不进行模型实验。

## 协作、升级与验收

Trae 实际固定确认：`4ad86c1c6ee94ac6c11e402a831a9a0a0c8d066d` §15（0032、source-policies API、source-policy 关联任务的受限 Engine fencing）；`f61a233` §16（ToolAuthority._grant 的 pre-charge 最小接线）。SA02 已在 §15 收讫。没有编辑/导入其活动树或 Harness 文件，没有占用 0033。

在独立副本根执行 `make migrate`、`make lint`、`make typecheck`、`make verify TASK=SAV1-03`。注册目标包括本卡模式/作用域/版本/撤权矩阵、真实 31→32 升级/旧行/checksum/事务回滚，以及 SA01/02、旧 Engine/API/账户/mailbox/工具/外发/模型账本/只读策略和恢复回归。失败尝试保留在 evidence/super-agent-v1/SAV1-03；JSON schema 与 manifest 纳入源码指纹。

本卡没有真实安装、生产 verifier、平台订阅/监听、模型调用、Worker、通知发送或发布。SA04 持久入站、SA07 总预算、SA13 真实桥、SA17 Delivery、SA19 管理页面、SA20 常驻清扫与 SA21 全安全生命周期仍按各卡交付。工程矩阵 PASS 不替代完整 policy_security/live/长期门。完成后等待独立复验，不自动进入 SA04。
