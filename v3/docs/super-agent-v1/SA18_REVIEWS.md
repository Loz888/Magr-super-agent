# SA18：完整审查与受控 Draft PR

状态：IN_PROGRESS，尚未具备独立验收条件。固定 Harness 第43节已确认并接线；86项准备及62个正式独特目标有分版本通过证据，含真实浏览器和三处SIGKILL。另有1项实际异步控制组合失败：waiting_approval 阶段原SQL拒收合法限制。拟0051已在隔离草案验证，正式落地等待新编号确认，见[具体请求](sa18-proposal/async-intake-repair-request.txt)。最终工程摘要尚未签发，独立验收 NOT_RUN。SA19 未启动。

## 开放范围

默认不登记 Profile、不启动模型、Worker、Dispatcher、监听器或发布。全部真实平台、付费、外发、发布和长期运行为 NOT_RUN；本副本固定 H12 真实 Agent/MVP 的 FAIL 保留，另一 CLI 负责其验收。`allow_external_write`、旧 R5 许可和 Coding Mandate 都不会建立本模块的独立写授权。

本轮只组合已验收的受控 Source Coding：实际本地 Git/H03 工作区、隔离 Docker 测试和原 Verifier 事实，发布远端为显式注入的本地受控 Git/PR 端口。`fixture://github/...` 是受控回执标识，不是实际 GitHub PR。无 merge、部署或自动真实发布接口。

## 原有权威与正式接线

Harness 确认：09145fe0939b209c43addb0ae6bbbdcc6a6df9a2，第43节。0050完整 SHA256 为 851f7a0d8a6673aa2f6416536d4f0629cc9d33d59541443e2a04bc88a10b751a，Engine/API 两文件原补丁为 442b0f1778280d9e6ddee04026c0ab6dbc81697beee4493c69567d3141fdbc69。具体文件 before/after 见 sa18-proposal/ownership-confirmed.json。

原 Engine 同一持锁 PG 连接和 RLock 是唯一业务写者。新增六表只存审查、限时加密正文、展示/批准、原 Ticket 的 PR/CI 关系和无正文审计；原 Run/Action/Approval/Outbox 与 repair_publication_steps 仍拥有业务及三步发布状态。文件与远端调用不在业务 SQL 事务中。

Source Coding 不建立旧发布 primary 订阅。本模块在 Owner 首次准备审查的原事务中创建缺少的原 subscriptions 行，并重核当前 Mandate/来源/原目标；不恢复 inactive 行、不替换其他 primary。撤销该订阅或丢失目标 fencing 后不能再发。

正式目录现有47份迁移，包括0050；主库只读核对仍为44份，0048/0049/0050均未安装主库。测试由原迁移入口安装到独立 PG；不得用主库或 Harness 的库代替测试。0051以后未预占。

## 准备、完整展示和精确批准

服务端先明确登记 ReviewProfile：受控仓库 ID、base 分支、Coding profile hash、Owner actor、独立有效期、H03采集器和分开的读/写端口。禁止从 HTTP 注册 provider、凭据或文件路径。缺单独写授权可以审查及导出，不能批准发布。

`POST /api/v3/source-reviews` 只接受 request_id、coding_id、verification_id、expected_epoch、expected_input_revision 和已登记 profile_sha256。程序检查当前 Source/canonical input、角色、四层策略、原 Mandate、最新已完成 attempt、真实 passed Verifier、有效测试数、环境/验收/代码树、无未决工具或 Job，以及现有异步 input revision。

采集从固定 H03 注册和原 manifest 读取实际基线/已测树；前像使用直接 SHA256，不能二次 hash。所有路径逐段 NOFOLLOW，拒绝符号/硬链接、可执行模式、二进制、越界、遗漏和额外文件。首片支持既有 UTF-8 普通100644文件，全树≤1MiB、单文件≤256KiB、最多16个变化文件；新增/删除和不能完整展示的变化失败关闭。

封装原 ReviewBundle：完整 Diff、变化后文件、仓库/base/branch、实际已测树和精确 payload hash，关联原 Verifier 回执/测试/环境/验收/来源/epoch/async版本。内容加密短期保存；普通 Action 和审计不复制原文、日志或代码。发布正文仅列最小任务与验证引用。

已登录 Owner 打开 `/source-reviews`。选择待审记录后，`POST /{review_id}/inspect` 完整呈现有界 Bundle，并保存同 Owner/会话/上下文/双 hash 的短时展示回执（≤120秒），不声称证明人已阅读。`POST /{review_id}/approve` 必须携带 presentation_id、bundle_sha256 和 action_sha256，再核当前权限、实际已测树及单独写授权，才在原事务建立原 Approval/Outbox。

普通 IM“同意”、模型输出、NotificationGrant、旧通用 Engine.approve 或 CLI token 不替代 Owner 的精确展示/批准。所有新路由使用现有 Owner cookie，变更要求 Origin/CSRF；私有页面、静态资源和响应 no-store/CSP，源码以文本渲染。已派发、unknown或已结算对象只展示无正文状态/受控回执；unknown提供只读核对。

## 派发与 unknown

`POST /tick` 是显式受控试验入口，经原 Dispatcher 领取 draft_pr。存在其他子系统可派发的 fixture Draft 时返回 OTHER_DISPATCHER_REQUIRED，不消费它们的动作。没有后台自动启用本模块的入口。

私有适配器复用原 Workbench 三步骤协议与原 repair_publication_steps。每步先在原事务保存请求 hash，再检查远端 base/branch/head；之后检查当前 Owner 会话、Source/角色/策略、原目标、input/epoch/async revision、最新 Verifier、实际工作区与原审批/primary/fencing，提交后才调用写端口。创建分支钉住具体 base commit；提交和 Draft 创建前拒绝已改变的远端 head。

接受远端回执需匹配完整 tree、全部 blob、唯一父 base、完整标题/正文、Draft状态、仓库和 PR head/base；仅变化文件匹配不足。来源撤权、编辑/删除、策略/角色/Coding/Async变更在原事务取消待发并擦除内容。已派发/unknown及真实旧回执保留，不声称取消撤销远端效果。

远端响应丢失或进程死亡保留 outcome_unknown，不自动重发或续发其余步骤。`POST /{review_id}/reconcile` 只调用独立 reader；没有分支、只有分支/提交、对象不同或无可靠结果都继续 unknown。完整对象一致才经原 Engine 结算。最小 hash 元数据支持正文已擦除之后核对旧副作用，不需要解密或读取工作区，也不续签审批或重置预算。

## 原生 PR/CI 与原 Ticket

`POST /{review_id}/native-links` 接受当前 SA05 SourceInputRef 和双 hash，重验真实规范化的 ResourceEvidence；不用正文 URL、模型建议或任意 caller receipt。仓库 ID/名字、PR号、head、base、安装和原 Ticket/Run 必须匹配。

SA05 CI本身不捏造 base SHA：先由同安装的当前规范化 PR事件锚定确切 head/base，再关联该 head 的 workflow/check 与 attempt。旧锚点撤回、force-push、base变化或跨安装都不能继承旧验证。关系不授予读取、执行或发布权限；受控远端的这些关联仍标 fixture，不能称真实平台验收。

SA17 review_available 仍只发最小“可审查”提示并要求其独立 NotificationGrant；不复制本 Bundle 的代码、私密日志或原消息，不代替批准。

## 验证与回退

`make verify TASK=SAV1-18-PREPARE` 是历史准备注册（86 PASS）；`SAV1-18` 是首批正式14目标；`SAV1-18-BOUNDARIES` 是首次新增边界。修复后只运行失败 nodeid/失败静态目标，新的首次检查另记；不重复此前通过项，不把分版本通过拼成最后源码单轮全量通过。

每轮先提交并 git archive，校验归档与工作树官方源码指纹一致，再执行；原失败与临时资源清理记录在 evidence/super-agent-v1/SAV1-18/。源内容和真实平台/模型钥匙不进入公开摘要。真实或长期门缺许可/未实测维持 NOT_RUN。

回退先停显式试验入口、撤销服务端 Profile，保留原 Outbox/步骤/unknown 回执并只读对账；不删除历史迁移或旧事实，不把 unknown 改 ready。不要直接卸载0050表或恢复旧备份来复活正文。完整安装、备份和常驻运维在SA20/21/23范围另验。
