# SA18：完整审查与受控 Draft PR

状态：IN_PROGRESS，受控实现及第43/44节确认范围已正式接线，独立验收 NOT_RUN。0051 的正式失败项复验和剩余首次检查见[当前工程交付记录](accept/SAV1-18/accept.json)；此前失败和分版本成绩保留，不能解释为最后源码单轮全量通过。SA19 未启动。

## 开放范围

默认不登记 Profile、不启动模型、Worker、Dispatcher、监听器或发布。全部真实平台、付费、外发、发布和长期运行为 NOT_RUN；本副本固定 H12 真实 Agent/MVP 的 FAIL 保留，另一 CLI 负责其验收。`allow_external_write`、旧 R5 许可和 Coding Mandate 都不会建立本模块的独立写授权。

本轮只组合已验收的受控 Source Coding：实际本地 Git/H03 工作区、隔离 Docker 测试和原 Verifier 事实，发布远端为显式注入的本地受控 Git/PR 端口。`fixture://github/...` 是受控回执标识，不是实际 GitHub PR。无 merge、部署或自动真实发布接口。

## 原有权威与正式接线

Harness 确认：09145fe0939b209c43addb0ae6bbbdcc6a6df9a2，第43节。0050完整 SHA256 为 851f7a0d8a6673aa2f6416536d4f0629cc9d33d59541443e2a04bc88a10b751a，Engine/API 两文件原补丁为 442b0f1778280d9e6ddee04026c0ab6dbc81697beee4493c69567d3141fdbc69。具体文件 before/after 见 sa18-proposal/ownership-confirmed.json。

原 Engine 同一持锁 PG 连接和 RLock 是唯一业务写者。新增六表只存审查、限时加密正文、展示/批准、原 Ticket 的 PR/CI 关系和无正文审计；原 Run/Action/Approval/Outbox 与 repair_publication_steps 仍拥有业务及三步发布状态。文件与远端调用不在业务 SQL 事务中。

Source Coding 不建立旧发布 primary 订阅。本模块在 Owner 首次准备审查的原事务中创建缺少的原 subscriptions 行，并重核当前 Mandate/来源/原目标；不恢复 inactive 行、不替换其他 primary。撤销该订阅或丢失目标 fencing 后不能再发。

Harness 第44节确认：162b6ef890f18c89df07adf62b0ca0375b5da69c。正式0051与已确认草案逐字节一致（SHA256：2bfcba77da3ad089737e1ae9fcb9db1aef72ca844e27c313c57a9686b55debd5），保留其中准备阶段的历史注释；正式性以迁移目录及独立确认记录为准，不改写已确认字节。

0051仅允许已验证的原任务在 waiting_approval 阶段记录合法异步控制元数据；原事务分配新 revision 后失效并擦除旧审查包。它不改变 sa_async_current、Worker/Job 启动、checkpoint/applied或模型/工具执行守卫，不恢复执行或产生批准。0047/0050及两文件补丁不变。

正式目录现有48份迁移，包括0050/0051；主库只读核对仍为44份，0048至0051均未安装主库。测试由原迁移入口安装到独立 PG；不得用主库或 Harness 的库代替测试。0052以后未预占。

## 准备、完整展示和精确批准

服务端先明确登记 ReviewProfile：受控仓库 ID、base 分支、Coding profile hash、Owner actor、独立有效期、H03采集器和分开的读/写端口。禁止从 HTTP 注册 provider、凭据或文件路径。缺单独写授权可以审查及导出，不能批准发布。

`POST /api/v3/source-reviews` 只接受 request_id、coding_id、verification_id、expected_epoch、expected_input_revision 和已登记 profile_sha256。程序检查当前 Source/canonical input、角色、四层策略、原 Mandate、最新已完成 attempt、真实 passed Verifier、有效测试数、环境/验收/代码树、无未决工具或 Job，以及现有异步 input revision。

采集从固定 H03 注册和原 manifest 读取实际基线/已测树；前像使用直接 SHA256，不能二次 hash。所有路径逐段 NOFOLLOW，拒绝符号/硬链接、可执行模式、二进制、越界、遗漏和额外文件。首片支持既有 UTF-8 普通100644文件，全树≤1MiB、单文件≤256KiB、最多16个变化文件；新增/删除和不能完整展示的变化失败关闭。

封装原 ReviewBundle：完整 Diff、变化后文件、仓库/base/branch、实际已测树和精确 payload hash，关联原 Verifier 回执/测试/环境/验收/来源/epoch/async版本。内容加密短期保存；普通 Action 和审计不复制原文、日志或代码。发布正文仅列最小任务与验证引用。

已登录 Owner 打开 `/source-reviews`。选择待审记录后，`POST /{review_id}/inspect` 完整呈现有界 Bundle，并保存同 Owner/会话/上下文/双 hash 的短时展示回执（≤120秒），不声称证明人已阅读。`POST /{review_id}/approve` 必须携带 presentation_id、bundle_sha256 和 action_sha256，再核当前权限、实际已测树及单独写授权，才在原事务建立原 Approval/Outbox。

普通 IM“同意”、模型输出、NotificationGrant、旧通用 Engine.approve 或 CLI token 不替代 Owner 的精确展示/批准。所有新路由使用现有 Owner cookie，变更要求 Origin/CSRF；私有页面、静态资源和响应 no-store/CSP，源码以文本渲染。已派发、unknown或已结算对象只展示无正文状态/受控回执；unknown提供只读核对。

## 派发与 unknown

`POST /tick` 是显式受控试验入口，经原 Dispatcher 领取 draft_pr。存在其他子系统可派发的 fixture Draft 时返回 OTHER_DISPATCHER_REQUIRED，不消费它们的动作。没有后台自动启用本模块的入口。

私有适配器复用原 Workbench 三步骤协议与原 repair_publication_steps。每步先在原事务保存请求 hash，之后重新采集并匹配实际已测工作区，再读取远端 base/branch/head 及所需完整对象证明，最后在原 Engine 事务重查 Owner 会话、Source/角色/策略、原目标、input/epoch/async revision、最新 Verifier 与原审批/primary/fencing，提交后才调用写端口。文件/远端回调均早于最后 SQL 权限复核，且不在 SQL 事务内执行。创建分支钉住具体 base commit；采集期间远端推进也会阻止下一次 commit 或 Draft 写入。SA18-I01 的本地定向修复记录见[专项报告](SA18_I01_REPAIR_20261115.md)，独立复验另行放行。

接受远端回执需匹配完整 tree、全部 blob、唯一父 base、完整标题/正文、Draft状态、仓库和 PR head/base；仅变化文件匹配不足。来源撤权、编辑/删除、策略/角色/Coding/Async变更在原事务取消待发并擦除内容。已派发/unknown及真实旧回执保留，不声称取消撤销远端效果。

远端响应丢失或进程死亡保留 outcome_unknown，不自动重发或续发其余步骤。`POST /{review_id}/reconcile` 只调用独立 reader；没有分支、只有分支/提交、对象不同或无可靠结果都继续 unknown。完整对象一致才经原 Engine 结算。最小 hash 元数据支持正文已擦除之后核对旧副作用，不需要解密或读取工作区，也不续签审批或重置预算。

## 原生 PR/CI 与原 Ticket

`POST /{review_id}/native-links` 接受当前 SA05 SourceInputRef 和双 hash，重验真实规范化的 ResourceEvidence；不用正文 URL、模型建议或任意 caller receipt。仓库 ID/名字、PR号、head、base、安装和原 Ticket/Run 必须匹配。

SA05 CI本身不捏造 base SHA：先由同安装的当前规范化 PR事件锚定确切 head/base，再关联该 head 的 workflow/check 与 attempt。旧锚点撤回、force-push、base变化或跨安装都不能继承旧验证。关系不授予读取、执行或发布权限；受控远端的这些关联仍标 fixture，不能称真实平台验收。

SA17 review_available 仍只发最小“可审查”提示并要求其独立 NotificationGrant；不复制本 Bundle 的代码、私密日志或原消息，不代替批准。

## 验证与回退

`make verify TASK=SAV1-18-PREPARE` 是历史准备注册（86 PASS）；`SAV1-18` 已登记8个正式测试文件供独立验收，含原首批14目标和后续首次边界；历史轮次保留当时登记内容。`SAV1-18-FINAL-REMAINDER` 只登记原异步失败1项及尚未执行的48迁移/最终注册2项。修复后只运行失败 nodeid/失败静态目标，新的首次检查另记；不重复此前通过项，不把分版本通过拼成最后源码单轮全量通过。

每轮先提交并 git archive，校验归档与工作树官方源码指纹一致，再执行；原失败与临时资源清理记录在 evidence/super-agent-v1/SAV1-18/。源内容和真实平台/模型钥匙不进入公开摘要。真实或长期门缺许可/未实测维持 NOT_RUN。

回退先停显式试验入口、撤销服务端 Profile，保留原 Outbox/步骤/unknown 回执并只读对账；不删除历史迁移或旧事实，不把 unknown 改 ready。不要直接卸载0050表或恢复旧备份来复活正文。完整安装、备份和常驻运维在SA20/21/23范围另验。
