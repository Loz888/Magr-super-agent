> 当前SA08已由用户工程放行：原3项独立PASS，新增权限/撤权/别名/无副作用6 PASS；未重跑历史全量。见[关闭记录](accept/SAV1-08/independent-closure-20261112.json)。下方为历史候选交付记录。

# SA08 确定性过滤与影子观察

状态：IN_PROGRESS。用户已接受 SA07-I01 两项独立复验并授权本卡，见
[关闭记录](accept/SAV1-07/i01-independent-closure-20261112.json)。Harness 固定提交
`1e397ab73feeaa6e9a5d747963bb9889eec5a51f` 的交接文档第31节已实际确认本卡共享范围。
私有规则、观察服务、0038迁移和共享接线已随本地提交
`c7214a7cf9615a8f8f8381094348feb1c34a874a` 固定，六个管理接口已注册。

首次整合 lint、typecheck 均 PASS，冻结首轮完整注册1358 PASS。随后本地发现SA08-L01富文本
控制识别问题，原3项FAIL确认后以`9950ebd`修复，原断言迁入正式注册并定向3 PASS；已通过项
未重复。当前注册1361项，修复后全量NOT_RUN。见[工程交付](SA08_ACCEPTANCE_20261112.md)
及[正式摘要](accept/SAV1-08/accept.json)。独立验收NOT_RUN，SA08仍IN_PROGRESS，SA09未启动；
真实平台、付费、外发、发布与长期门均NOT_RUN。本地交接见[SA08交接说明](HARNESS_SA08_HANDOFF.md)。

## 来源与权限

输入必须是原SourceIngress的canonical input。服务在原Engine持锁连接/事务中重新检查
当前head、删除/歧义/过时、TTL、owner、Source/identity/policy revision与Receive。
只有当前允许且仍保留有效密文的内容才在内存中做有界确定性解码；候选还要求model_input。
没有正文时不补拉、不跟随链接，也不复制正文到观察日志、CAS或新表。

Ingress hook只对当前receipt新建的canonical input保存一次无正文origin。历史、缓冲、
缺口和时效原因来自受信服务端参数，不在HTTP DTO中开放。别名不能改写origin，也不能在
开启功能后自动激活先前未观察的输入；原有canonical若没有可信origin，保守拒绝推断为live。
管理员可以显式提交已有input ID重新请求观察，但既有决策不可变、当前权限仍需复查。

FastPathSettings默认disabled。模式来自现行管理员CAS签署的PolicyDefinition；本卡不提供
环境变量或正文切换模式的方法。即使策略为auto_investigate/auto_repair，SA08仍只保存
would_*假设，不调用旧Engine.ingest、Routing.prepare/commit或业务控制/执行/外发端口。

## 规则与候选

GitHub和飞书使用其既有规范化内容格式。人类的可信mention/direct或严格的
`/mgar investigate|repair|ask`语法可产生显式请求候选，身份与Admission独立检查。
普通ambient内容只有管理员签署的team_rule允许时才成为待评估观察；这不表示语义相关。
非人类永远不能成为人类请求者或控制者，但允许的自动化在准确签署规则下可would_observe。

`/mgar pause|cancel <ticket UUID>`只提供意图。当前人类身份、task_controller、准确Ticket
membership及follow_up Admission全部成立后，记录优先级100的would_pause/cancel，
不实际暂停或撤销任务。飞书post的at元素规范成固定`@_user_0 `，不读取其显示名/ID作为命令；
已验证mention才允许剥一个前置`@_user_N`占位符。任意显示名、
引用文本或调用者priority字段不能提供控制权。控制观察优先于普通候选冷却与候选配额，
但不绕过已有SourceGateway来源配额、ACK或积压边界。

来源配置中的own_actors只接受同SourceScope的bot/service。自身PR/CI还可通过既有Action或
Workbench的已知成功live回执，按当前项目/仓库与准确PR编号核对；支持原数字回执及GitHub
PR URL。unknown、fixture回执、其他PR及正文中的“自身结果”标记不能提供该事实。
有效控制意图的授权检查先于此反馈过滤。

同一安装、会话、原生话题且授权快照一致的合格输入可在有界窗口内合并。没有原生话题时
用实体键隔离，不把整个群当作话题。每个新普通batch消耗一次既有共享candidate配额，
同batch新增成员和同canonical重投不重复占用；冷却与历史决定保留在PG中。预算未配置、
额度不足或容量不足都有独立原因，Router语义评估始终NOT_RUN。

## 容量与可见性

默认cooldown_seconds=30、merge_window_seconds=10、max_batch_items=16、
max_decisions=10000、max_batches=1000。决策和batch容量分别按普通优先级0、控制优先级100
两个池限制，合计硬上界分别为配置的两倍，为已获准控制保留容量。origin最多一个/既有
canonical input，受原入站凭证/输入上界约束。观察池满时保留原input pending，不因观察
容量回滚已经持久接收的事件或其必要隐私失效；原入站积压仍提供背压。

只有持久保存决策后才标记原input.consumed_at，含义是Fast Path已观察，不是Worker已应用。
新日志只存内部input/batch ID、hash、授权revision、固定reason与would_*。预览重新鉴权，
返回当前有效成员、过时原因和绑定成员版本的snapshot_sha256；旧快照不是继续执行凭证。
触发/拒绝/模式/优先级分布来自实际决策；SA08模型调用与费用为0，不生成模拟测试或发送回执。

## 管理接口与集成

fast_path_api.py已通过主API注册以下六个接口，沿用控制台cookie、写操作Origin/CSRF、
事务内当前会话复查与no-store：

| 方法 | `/api/v3/source-fast-path` 下的路径 | 内容 |
| --- | --- | --- |
| PUT | /{source_id} | expected_revision、settings、review_ref的签署CAS保存 |
| GET | /{source_id} | 当前设置及revision |
| POST | /{source_id}/observe | 仅允许已有input_ids；没有正文或权限断言字段 |
| GET | /{source_id}/preview | 最多100个有界当前/历史batch预览 |
| GET | /{source_id}/decisions | 最多100条无正文决定 |
| GET | /{source_id}/metrics | 实际触发分布、pending和零模型用量 |

已应用的共享变化仅为Engine.__init__组合、api.py注册和SourceIngress新canonical观察hook，
继续使用原Engine mutex/advisory lock、持锁PG连接及已有事务。复用既有SharedBudgets.quota
的candidate入口；预算/结算算法、ToolAuthority、SourceEvent DTO与Harness实现均未改。

0038已落入本树迁移目录，目录共有37份迁移，即0001–0036及0038。
0037_harness_evaluation_budget.sql继续归Harness，本树不导入、不改名、不伪造占位；
历史0001–0036字节保持。已确认并应用的[共享接线](sa08-proposal/shared-wiring.patch)及
[0038 SQL](sa08-proposal/0038_super_agent_fast_path.sql)保留在提案目录。

用户代转了实际确认提交，已只读核对固定第31节及两份本地提案hash。确认全文SHA256为
`036090e77a01340c3e4652a470442ee8ae21c2020fe1204ac66db95d6abf4dc5`，
SQL为`7f03acdf8e428a0cb621ab555d3b04d44f7dc76127f4e5eedf0c23a698466eb6`，
接线补丁为`3269bc6c33ba572c9057a345bba0c1c73119be772686a2be7af1ecb0dc1422f7`。
实际确认及读取时间记录于
`evidence/super-agent-v1/SAV1-08/start-20261112T081641Z-5eb84f0a/ownership-confirmation.json`。
Harness明确不并行修改上述SA接线；本卡未修改或导入其活动树，新增本地交接文档未再次发送消息。

## 验证范围

纯规则测试与合成确定性基准不表示独立语义holdout。SA00冻结的200条dev/holdout协议仍需
操作者复核，SA09才接入获准语义评估。首次完整注册197项新增检查及1161项既有回归
共1358 PASS/exit0，源码37a618…0898b前后一致；修复9950ebd/69ddeb…47f27仅复测
原3项失败，结果3 PASS/exit0。当前注册200项SA08检查及1161项既有回归，共1361项；
修复后完整注册及已通过的lint/typecheck未重复。两版本证据分别记录，不拼为单轮全量成绩。

## 历史准备与协调记录

以下为实际第31节确认之前的阶段记录，原失败与未实测范围继续保留。

初期私有规则、观察服务、管理接口模块及测试已编写；共享接线和0038迁移尚待所有权确认，
未安装或注册。当时只有纯规则/契约组件56项PASS及组件静态检查，真实PG集成与本卡完整
注册验证未运行，这些组件结果不构成本卡完成或独立验收。

当时拟议共享变化为Engine.__init__组合、api.py注册和SourceIngress可选新canonical观察hook。
[共享接线](sa08-proposal/shared-wiring.patch)、[0038 SQL](sa08-proposal/0038_super_agent_fast_path.sql)
及[具体协调请求](sa08-proposal/ownership-request.txt)仅作为可审查草案，尚未应用到源码接线
或迁移目录。0037已实际归Harness，0038尚未确认。

原协调请求位于
`evidence/super-agent-v1/SAV1-08/ownership-read-20261112T081530Z/ownership-request-draft.txt`。
当时跨CLI消息工具缺失，受支持daemon proxy未响应；请求已交用户代转，尚未取得对方实际
确认。后续只读复查未找到该确认的记录也按各自时点保留；实际第31节确认在其后单独核验。
