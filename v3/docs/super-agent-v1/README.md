# MAGR Super-Agent 落地任务书

当前更新（2026-11-17，停服恢复与真实检索验证）：**SA22 IN_PROGRESS**。
旧24小时临时配置于今天12:04到期，接收进程退出且状态遗留在线，已恢复同一范围。
问答至11月24日15:03、编码实验独立至11月18日15:07（上海时间），原累计USD2不变。
退出状态现写offline/expired；实际停止和重启核对通过，编码到期不再拖停问答。
本人重新发送的源码问题和连续追问均实际回复：2个样例、6次真实模型请求、各Outbox
只派发1次，引用实际源码行，第二轮加载上一轮上下文。累计16次、USD0.018484/2，
unknown/预占0。未重跑原通过工程项，产品源仍39e151e，主库44／接入PG51，无新SQL。
见[恢复及真实闭环报告](SA22_REPLY_RECOVERY_20261117.md)与
[机器摘要](accept/SAV1-22/reply-recovery-20261117.json)。这两条样例不代替独立语义质量、
完整SA22六门或长期运行，H12质量FAIL保留，SA23未启动。

以下保留此前启用／待实测与旧到期日等历史记录，不代表本轮当前配置。

当前更新（2026-11-16，自动仓库检索／追问承接）：**SA22 IN_PROGRESS**。
已在Bot群启用只读目录／代码搜索／按行读取和实际源码引用，最多承接同群／话题本人
最近3轮获准问答，服从30分钟正文期限及传递撤权。新24项目标初22PASS/2FAIL，
仅原2项复验2PASS；lint/typecheck339文件通过，未重跑原通过项。实际目标仓库已自动
读到memory三文件片段；新模型效果与连续追问回执待新群消息核对，不提前记PASS。
原累计10次真实问答、USD0.010509/2，unknown/占款0；预算、Coding许可及到期时间未改。
见[本轮交付](SA22_REPOSITORY_ASSISTANT_20261116.md)及
[机器摘要](accept/SAV1-22/repository-assistant-20261116.json)。主库44／接入PG51，无新SQL
或共享Engine/API/费用算法变更，H12质量FAIL保留，完整SA22六门未放行，SA23未启动。

以下保留早期固定上下文／单轮问答等历史状态，不代表本轮当前启用范围。

当前更新（2026-11-16，模型／编码接线）：**SA22 IN_PROGRESS**。
按负责人明确授权的H12质量门例外，已启用Bot群本人真实模型问答与隔离编码实验入口；
长连接/API及供应商只读模型列表核对通过，当前推理0次、费用USD0/2。真实问答回复、
实际编码产物与动态答案回执仍待新群消息实测，均不提前记PASS。20个新目标有分版本
通过证据，已通过项未重跑；实际原Broker运行固定smoke 1PASS。0054按Harness efb09f4
第48节落地，主库仍44、专属接入PG51；预算未重置，H12质量FAIL/MVP未放行保留。
见[本轮接线与证据](SA22_MODEL_CODING_20261116.md)及
[机器摘要](accept/SAV1-22/model-coding-20261116.json)。完整SA22六门未放行，SA23未启动。

以下保留此前回复／接入阶段历史记录，旧“模型未开启”等描述不代表本轮当前配置。

当前状态（2026-11-16）：**SAV1-00–SAV1-21 DONE；SA22 IN_PROGRESS；SA23–SA24 PLANNED**。
Bot 群本人三条不同 @ 消息已取得真实固定回复，原 Outbox 各派发一次。新增回复检查11PASS、
typecheck通过；格式修正后只复验失败lint并通过，未重跑原通过项。模型问答和自动编码未开启，
新增费用USD0/2，H12原FAIL保留；完整SA22六门仍NOT_RUN。见[回复接通记录](SA22_REPLY_BACKEND_20261116.md)
及[当前状态](SA22_STATUS_20261116.md)。没有新增迁移或共享Engine/API/schema/预算算法改动。

以下保留此前仅接收、尚未接通回复的引导阶段记录：

状态：**SAV1-00–SAV1-21 DONE（用户接受的受控本地独立闭环）；SA22 IN_PROGRESS（真实试用准备/飞书接入引导）；SA23–SA24 PLANNED**。更新：2026-11-16。

SA21 候选5be5dc7/交付1ec8941的原9项及新增3项独立12PASS已核对并获用户接受，见[独立关闭](accept/SAV1-21/independent-closure-20261116.json)。SA22按用户指令启动：试用目标为本机magr-harness对应Loz888/MAGR-v3、使用者仅本人、模型共享总上限USD2且外发范围为仓库代码。飞书“MAGR助手”已创建/发布/加入Bot群，长连接及真实平台身份已核对，身份绑定后的定向接收产生1份accepted原收件箱输入；此前2份拒收保留。当前Shadow、不保留正文、0模型/0外发，自动回复与任务链未接线；SA22完整六门NOT_RUN，H12原FAIL保留。见[接入状态与剩余工作](SA22_STATUS_20261116.md)及[准备记录](accept/SAV1-22/preparation-20261116.json)。主库仍44，本次专属接入PG安装既有50份SQL；没有重跑历史通过项。

以下保留SA21待独立验收时的工程交付与更早阶段历史状态：

状态：**SAV1-00–SAV1-20 DONE（用户接受的受控本地独立闭环）；SA21 IN_PROGRESS（本地受控工程已交付，待独立验收）；SA22–SA24 PLANNED**。更新：2026-11-15。

SA20独立原失败1PASS已核对并获用户接受，见[关闭记录](accept/SAV1-20/independent-closure-20261115.json)。按用户指令推进SA21，候选5be5dc7的25个新组合检查具分版本PASS：初轮16PASS/9FAIL，夹具修正后仅原9项9PASS，不重复通过项，不称最终单轮全量通过。见[工程交付](SA21_ACCEPTANCE_20261115.md)、[数据保留/恢复策略](SA21_SECURITY.md)及[机器摘要](accept/SAV1-21/accept.json)。SA21等待独立，SA22未启动；主库仍44，真实门NOT_RUN，固定H12 FAIL保留。

以下保留SA20修复与更早阶段记录：

SA20-I01独立P2已确认：实际Coding完成事实未计入Watchdog进展。私有修复候选7b4566d仅原失败项1PASS/exit0；九次实际完成均更新时间和游标，原探针字节/断言未改。原13独立、55正式/88准备和静态通过未重跑，原失败保留；见[修复报告](SA20_I01_REPAIR_20261115.md)及[机器记录](accept/SAV1-20/i01-repair.json)。SA20未放行，等待独立复验；主库仍44，SA21未启动，全部真实门NOT_RUN，固定H12 FAIL保留。

以下保留原工程交付与历史阶段记录：

SA19 045934c/40bbbcf的I01独立原三个失败参数3PASS已核对并获用户接受，静态资源均拒绝且no-store，原探针不改、其他通过及旧失败保留；见[独立关闭](accept/SAV1-19/independent-closure-20261115.json)。按指令启动SA20常驻部署/Watchdog/运维，先审计既有进程、身份、租约、源健康和备份边界，共享范围另行精确协调。SA21未启动，主库仍44，真实门NOT_RUN，固定H12 FAIL保留。

SA20当前：第46节1c4f494已核对，正式0053四表、原Engine/API/Settings接线与11接口及独立常驻控制进程、HTTPS、加密备份恢复已交付。55个正式独特用例与原88准备项有分版本PASS，修复仅复验失败项/目标；见[工程交付](SA20_ACCEPTANCE_20261115.md)、[运维手册](../../deploy/super-agent-v1/README.md)及[逐例证据](accept/SAV1-20/accept.json)。整卡IN_PROGRESS等待独立验收，SA21未启动；主库仍44，真实门NOT_RUN，固定H12 FAIL保留。

以下为原阶段准备记录：

SA20当前：私有契约/纯信号/0053隔离草案70项与限权客户端18项均有分版本PASS，原失败仅定向复验、通过项不重跑；静态结果按版本保留。见[实施边界](SA20_OPERATIONS.md)／[70项准备](accept/SAV1-20/preparation-20261115.json)／[客户端摘要](accept/SAV1-20/client-preparation-20261115.json)。[0053四表/三文件/11接口具体请求](sa20-proposal/ownership-request.txt)待Harness新确认，正式SQL/共享接线未应用，部署与备份恢复演练NOT_RUN。SA21未启动，主库仍44，真实门NOT_RUN，固定H12 FAIL保留。

以下保留历史阶段记录：

SA19-I01独立P2（非空ASGI root_path绕过私有静态保护）已确认并本地定向修复。候选045934c仅原三个失败参数3PASS/exit0，HTML别名GET为404、JS/CSS HEAD为405，均no-store；原探针字节/断言未改，其他33独立通过、历史78正式/54准备与静态通过目标均未重跑。见[修复报告](SA19_I01_REPAIR_20261115.md)及[机器记录](accept/SAV1-19/i01-repair.json)。SA19保持IN_PROGRESS、未放行，等待独立复验；SA20未启动，主库仍44，真实门NOT_RUN，固定H12 FAIL保留。

以下保留原交付与历史阶段记录：

SA18独立关闭已获用户接受。SA19第45节8d19308确认已核对，正式0052及原Engine/API/首页三文件按精确hash落地。候选7c2d768，78正式独特用例分版本PASS、54准备PASS保留，修复只复验失败项/目标，不称最终同版本全量通过；见[本地工程交付](SA19_ACCEPTANCE_20261115.md)与[逐例摘要](accept/SAV1-19/accept.json)。49仓库迁移/584产物引用匹配，主库仍44，无本卡临时资源残留。SA19等待独立验收，SA20未启动，真实门NOT_RUN，固定H12真实Agent/MVP FAIL保留。

SA18-I01独立P1（最后工作区采集后缺少远端head/base复查）已本地定向修复；候选ed3d24f仅原两个失败参数2PASS/exit0，writer均只有branch，无commit/PR写入，真实Git对象已核对。原探针/断言/失败证据保留，原15独立及历史68/86/静态通过未重跑。见[修复报告](SA18_I01_REPAIR_20261115.md)和[机器记录](accept/SAV1-18/i01-repair.json)。SA18保持IN_PROGRESS等待独立复验，SA19未启动，真实门NOT_RUN，固定H12真实Agent/MVP FAIL保留。

SA18 首轮历史工程候选 ecb9ede 已完成受控工程交付；第43/44节确认的0050/0051及原Engine/API接线落地。68个正式目标有分版本PASS证据，本轮仅原异步失败1项及首次迁移/注册2项共3PASS，原65通过未重跑，不称最终全量PASS。正式48迁移/550产物引用匹配，主库仍44；见[交付](SA18_DELIVERY_20261115.md)和[机器摘要](accept/SAV1-18/accept.json)。独立验收NOT_RUN，SA19未启动，全部真实门NOT_RUN，固定H12真实Agent/MVP FAIL保留。

SA10已由用户接受[独立原2项PASS及199历史证据核对](accept/SAV1-10/i01-independent-closure-20261113.json)，旧FAIL和分版本成绩保留；SA11动态订阅、Mailbox与交回也已独立闭环，首轮失败按失败范围定向修复并留存各版本证据。原任务状态/写者和Source撤权/TTL约束保持；全部真实/付费/外发/发布/长期门NOT_RUN。
SA11第35/36节固定确认已核对，0042/0043及七表、11接口和四文件接线正式落地，SA11交付时41checksum一致。首轮309PASS/42FAIL保留，修复仅复验失败范围42→40→26→1，各新增2/14/25/1PASS，剩余0；未重复已通过项，不称最终351全量PASS。候选ca49b76，见[工程交付](SA11_ACCEPTANCE_20261113.md)与[分版本摘要](accept/SAV1-11/accept.json)。SA11已获用户接受独立放行，见[关闭记录](accept/SAV1-11/independent-closure-20261113.json)；SA12当前工程交付见下述记录。
仓库基线：`973994c0e6c087c2968b3d91a3a6b9220e2c0892`；该提交新增 Harness 规划，
应用功能基线仍为 `1d07285aa27ece9bae4ccbedc3a77ce2fbcbaf3a`。

本任务书承接用户粘贴的“Always-on Coding Harness + Event-Driven Super-Agent”需求，
并引用 [Harness V4 规划](../harness-v4/README.md)。Harness 当前能力以其负责人固定交付及独立验收为准，本轮不跟进或变更 H 系列结论；规划条目不能当作已具备的 Worker 能力。本文件不启用监听、不安装机器人、
不发送群消息，也不授予新模型或外部写权限；后续实施按各卡验收逐步开放能力。

SA12候选6b48b7d已完成本地工程；首轮421项416PASS/5FAIL，修正测试缺失探针后仅原5项5PASS，原416未重跑，不称最新单轮421PASS。第37节共享范围已落地，当前42checksum、七表0行及无PG/会话/容器残留核对通过，见[交付](SA12_ACCEPTANCE_20261113.md)与[分版本摘要](accept/SAV1-12/accept.json)。SA12原5独立5PASS及新增4边界4PASS已核对并获用户接受，见[独立关闭](accept/SAV1-12/independent-closure-20261113.json)；当前按用户明确指令启动SA13。

SA13具体准备与等待确认范围见[Worker接线说明](SA13_CODING.md)；纯组件成绩不代表整卡工程或真实Coding通过。

## 阅读入口

- [架构、领域、数据与权限契约](ARCHITECTURE.md)
- [25 张任务卡：范围、依赖、产物和验收](TASKS.md)
- [与 Harness 的合并路线、里程碑和估时](ROADMAP.md)
- [常驻部署、日常使用、故障和运维](OPERATIONS.md)
- [工程、真实渠道、编码能力与长期运行验收](ACCEPTANCE.md)
- [机器可读任务与需求映射](plan.json)
- [SA06 固定工程候选、完整证据与限制](SA06_ACCEPTANCE_20261111.md)
- [官方资料版本、审计依据与输入摘要](sources.json)

## 1. 要交付的产品

MAGR 持续接收明确授权的研发群与 GitHub 事件，识别值得跟进的问题，将同一问题的
日志、讨论、PR 和 CI 关联到已有任务；需要工作时分配独立 Coding Worker，在隔离
工作区内调查、复现、修改和验证。系统在有可用结果、需要补充信息或必须审查时，
按通知规则向原会话反馈，用户可在同一话题继续指示。

这是一套持续运行的研发协作系统：

- 感知层处理事件来源、身份、消息范围和断线。
- 控制层处理策略、路由、关联、订阅、预算和 Worker 分配。
- Harness 提供单任务的读/改/执行/验证、上下文与恢复能力。
- 交付层提供可核验报告、补丁、审批、原会话通知和受控 Draft PR。
- 运维层负责常驻服务、积压、失联、成本、数据保留和恢复。

### 首版范围

| 项目 | 首版目标 |
| --- | --- |
| 团队/管理 | 一个组织、一个控制台管理员；多个经绑定的来源用户具有不同请求/控制权限 |
| 来源 | 一个 GitHub 安装/仓库、一个飞书机器人与一个已授权研发群；指定用户、@、话题和事件筛选 |
| 编码 | 通过 Harness 验收的 Python/JS/TS 范围，独立工作区与实际测试；保留 Go 兼容回归 |
| 私有仓库 | 支持目标为 GitHub App 明确授权的仓库；私有读取、模型数据外发和结果可见性须独立验收，不能沿用现有公开仓库假设 |
| 自主性 | 影子观察 → 自动调查 → 隔离修复；按策略授权，不要求每次读写/测试都弹窗 |
| 通知 | 结果、必要提问、审查和重要故障；安静时段、合并、限流、原话题返回 |
| 交付 | 可验证报告/Diff/测试；Draft PR 需要现有精确提案审批；不默认合并或生产部署 |
| 并发 | 初始最多 2 个执行 Worker，值由部署与压测冻结；不同任务不共享可写目录 |
| 常驻 | 在线服务器负责接入、控制与至少一个云端 Worker；关闭浏览器或用户电脑后仍能工作 |

首版不要求每个任务再拆成多个模型角色；这里的多 Worker 主要服务不同任务的隔离
并发。Harness H17 的按需子 Agent 仍是后续可选项。

## 2. 与 DeepSeek Super-Agent 的关系

已核对 DeepSeek AI 组织下的
[dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/8c32a9dc5d72c033816c6a8110b37f40cf94fba4/README.md)。
其 README 明确标注：研究参考原型、不是正式 DeepSeek 产品、尚未生产就绪，公开
版本与内部版本及文章描述并不完全一致。本规划固定该提交作为参考，不把文章里的
方向性设计当作已经验证的产品能力。

可借鉴的是 Sources、Trigger/Thread/Ticket/Mandate、Router–Worker、订阅直通、
后台 Job、节制通知及结果回原会话的分层。MAGR 保持现有 Engine/PG 业务权威、
实际执行隔离、预算预占和未知结果对账机制。原型文档中“以操作者 OS 身份执行
命令”、部分恢复和重试规则需要按 MAGR 的边界重新实现与测试。

## 3. 规划冻结时的基础与缺口（历史快照）

下表描述最初规划基线；当前来源/身份/策略/入站工程进展以文首链接为准，不代表已开放真实平台或 Worker。

| 能力 | 已存在的基础 | 本次必须补齐的部分 |
| --- | --- | --- |
| 入站事件 | GitHub Webhook 验签，Issue 评论和部分 workflow 失败分支 | 通用 Source 契约、安装/租户隔离、飞书、PR/CI 完整关系、缺口与重放 |
| Thread/Run | PG threads/tickets/runs/mandates，事件去重 | IM 原生话题、无话题群聊、跨渠道关联；不伪造 issue_number |
| Router | Routing 子服务、模型提案、版本/epoch 校验、router_inbox | 自然语言事件评估、影子模式、明确团队规则下的主动任务 |
| 订阅与收件箱 | primary/observer/context_only、Mailbox 和只读投递 | TTL、动态订阅、交回/取消、晚到消息、关闭后跟进、ACL 重校验 |
| 主动策略 | CI 失败、依赖更新、PR 评论的受限 policy/speculation | 四层权限、日/项目总预算、影子→自动状态、完整 Coding Worker 接线 |
| Worker | 后台 Run、修复组件、快照和沙箱 | Harness V4 的真实编码闭环、独立分配/租约/进程、异步消息与版本一致恢复 |
| Recall | 有界同范围候选、Memory、证据/CAS | 可遍历来源→话题→任务→运行→结果，逐跳鉴权与过期/删除处理 |
| 外发 | Action/Approval/Outbox/Dispatcher、已知/未知回执 | 飞书/GitHub 通知通道、预授权通知策略、抑制噪声、内容披露与对账 |
| Watchdog | 心跳、进展、Mailbox/Router/Outbox 积压及依赖观测 | 来源健康、漏接区间、调度公平、工作区/进程回收、常驻部署与演练 |
| 用户界面 | 登录、模型设置、分析历史、SSE | 来源/群用户配置、Shadow 预览、任务关系、通知、费用、暂停/撤权与审查 |

审计约束：当前 `TriggerInput` 仅支持 github/cli/web，要求 repository 和
issue_number，身份会小写化；`Engine.ingest` 限定实例配置的仓库，Thread 键为
`repository#issue:n`。飞书身份与群聊不能直接套入这一契约。

现有 R3 边界要求显式请求，并对 unsubscribe/handback 保持限制；旧主动策略的 live
graph 主要是受限调查，不是通用代码修复。新增团队规则自主性和订阅生命周期需
正式 ADR、版本化契约和前向迁移，不能仅修改一个枚举或删除权限拒绝分支。

## 4. 贯穿实现的原则

1. **复用唯一控制平面。** Engine 是全局业务写者，PostgreSQL 是事实源；扩展现有
   Thread/Ticket/Run/Subscription/Outbox，不复制另一套同名业务系统。
2. **权限分层。** 能收到消息、能触发工作、能执行代码、能向某处反馈分别校验。
   平台白名单和订阅都不自动授予仓库写权限或管理员控制权。
3. **确定性入口先行。** 验签、安装/群/用户、重复/自身消息、时效和作用域先过滤，
   再在有预算时调用 Router。已有订阅可省略新任务评估，但不能省略权限校验。
4. **每个任务独立执行。** Worker 具有自己的 Session/Run、工作区、租约、上下文
   和受共享总额约束的预算；Router 不管理每次 Shell 或测试。
5. **静默有状态。** 跳过无关事件、无需每步回复；失败、等待、Shadow 和预算耗尽
   在控制台可见，不把没有声音解释成已经成功。
6. **副作用可追溯。** 通知、评论、PR 均经过有对应授权的 Action/Outbox；未知结果
   先对账。根据平台能力实现幂等，不承诺跨外部系统无条件 exactly-once。
7. **按证据开放自主性。** 先 Shadow 校准，再调查，再隔离修复；每一级都有范围、
   预算、失效条件、撤权和可回退的配置。

## 5. 从需求到首个真实闭环

目标示例，时间不代表实测：

1. 获准用户在指定研发群报告登录接口异常；系统校验接收与触发策略。
2. Router 在已有任务里查重，或按明确规则建立带 Mandate 的新任务。
3. Scheduler 给任务分配独立 Worker；Worker 在固定代码版本上调查、复现和修复。
4. 话题中的新日志与关联 PR/CI 作为有来源的输入送到同一任务；操作者新限制在
   安全点生效，不兼容的后续操作停止。
5. 通过实际验证后，生成报告、Diff 与回执；根据通知授权在原话题发一次可用结果。
6. 管理员审查精确变更后创建 Draft PR；后续 CI/追问沿同一引用链继续处理。
7. 用户关闭客户端不影响云端任务；若执行器只在已关机的本机，则任务明确等待
   Runner，不能把持续接入等同于仍在执行代码。

## 6. 交付顺序

Harness 与 Super-Agent 控制层分两条轨道：来源、策略、影子路由和界面可以在
Harness 接口冻结后并行开发；自动修改代码必须等待 Harness 的真实能力和恢复门。

首批任务是 **HV4-H00/H01 + SA00–SA04**：冻结状态/权限映射、Source 与身份契约、
策略、持久入站；之后接入 GitHub/飞书并在 Shadow 中校准。完整批次、依赖和估时见
[路线](ROADMAP.md)。SA00–12已获用户接受独立闭环；SA13已授权进入实施准备，SA14–24仍PLANNED。真实渠道、编码、发布和长期运行门仍 NOT_RUN。

SA13第38节617e14a已正式落地，0046及六接口、固定H11切片与受控纯函数Coding桥已交付；43checksum一致。正式初185项156PASS/29FAIL，定向修复仅29→3→2，分别26/1/2新增PASS；另27新边界首次PASS，未重跑原156及后续通过项，不称最新单轮212PASS。受控多轮真实工作区/容器修复、三SIGKILL与TTL/撤权/初始树门有工程证据；等待独立验收，SA14不启动。固定H12 live_agent/MVP仍FAIL，全部真实/付费/外发/发布/长期门NOT_RUN，见[SA13交付](SA13_ACCEPTANCE_20261113.md)及[分版本摘要](accept/SAV1-13/accept.json)。

SA13 f53f423／c49cf1f 已获用户接受独立本地受控验收，原29及新3独立32PASS；212分版本、43checksum及313refs核对一致，前两轮只有工作树指纹、无不可变归档的证据限制保留，未重跑历史通过。见[关闭记录](accept/SAV1-13/independent-closure-20261114.json)。现按用户明确指令启动SA14；H12真实Agent/MVP仍FAIL，真实/付费/外发/发布/长期门NOT_RUN，SA15不启动。

SA14私有契约／planner65PASS、SQL草案29项分版本通过，静态错误已定向修复；详见[当前状态](SA14_ASYNC.md)与[准备摘要](accept/SAV1-14/preparation-20261114.json)。正式迁移／接线／Runtime及整卡验收NOT_RUN，待新Harness确认[具体请求](sa14-proposal/ownership-request.txt)，SA15不启动。

SA14新确认[c711d545第39节](sa14-proposal/ownership-confirmed.json)已核对，正式0047和三文件按精确hash落地；服务／12路由／异步原Job与受控模型端口已实现，首次正式检查尚未运行。原私有65／草案29历史通过不重跑；H12由另一CLI验收，SA15不启动。

SA14当前：第39节确认c711d545，正式0047六表／三文件最小接线／12路由及异步原Job已交付。
首次正式61项26PASS35FAIL；定向修复原29项29PASS、原6项1PASS5FAIL，最后原5项及新增原子编辑边界共6PASS。
两个单失败诊断轮的超时保留。62个正式独特目标均有分版本PASS，未重跑通过项；原65／29准备证据不重跑。
[本地工程交付](SA14_ACCEPTANCE_20261114.md)／[逐例摘要](accept/SAV1-14/accept.json)。
整卡仍IN_PROGRESS、独立NOT_RUN，SA15不启动；H12由另一CLI验收，本副本固定真实门FAIL保留，其他真实门NOT_RUN。

SA14-I01最新：独立原35项35PASS保留，新增最终启动2FAIL为P1，独立未放行。
私有修复首候选934ddec的原2项仍FAIL，修正实际SA12 Broker authority接线后e88ff0c仅原2项2PASS。
原探针字节／断言／阈值未改，原35和全部历史通过及静态目标未重跑。
见[修复报告](SA14_I01_REPAIR_20261114.md)与[机器记录](accept/SAV1-14/i01-repair.json)；
等待独立复验，SA14保持IN_PROGRESS，SA15不启动；H12由另一CLI负责，全部真实门NOT_RUN。

SA14 598f760／cfc217e的I01独立两项2PASS已核对并获用户接受，原35／62／94及历史失败不重跑。
[独立关闭记录](accept/SAV1-14/independent-closure-20261114.json)。依用户指令启动SA15可鉴权Chain of Recall。
当前审计既有Context／Memory／CAS与固定H10，不把关系或摘要当授权；新Engine/API接线范围须另行精确协调。
H12由另一CLI验收；真实平台／付费／外发／发布／长期门NOT_RUN，SA16未启动。

SA15第40节e0432f0已核对，原Engine/API两文件精确接线及9个只读接口落地，不新增迁移/预占0048。
源码bc1f91c，110正式目标分版本PASS及89准备PASS保留；原3失败只3项复验，关系轮原1失败只1项复验，
其余为首次新检查，不重复已通过用例，不称最后候选单轮110/199全量PASS。44checksum/443refs一致，无临时资源残留。
[工程交付](SA15_ACCEPTANCE_20261114.md)／[逐例摘要](accept/SAV1-15/accept.json)／[接口说明](SA15_RECALL.md)。
静态PASS绑定列明版本，之后按用户要求未重复已通过静态目标；限制保留。整卡待独立保持IN_PROGRESS，SA16不启动。
H12由另一CLI负责，固定真实Agent/MVP FAIL保留；真实平台/付费/外发/发布/全系统安全/长期运行NOT_RUN。

SA15-I01独立P2：默认搜索续页时窗漂移。私有服务/内部加密游标已冻结首屏实际UTC边界，
候选45a79cf仅原失败函数1PASS/exit0，A/B返回而首屏后C排除；原探针字节/断言未改。
原独立4PASS/新权限3PASS、110正式/89准备及静态通过目标均未重跑，历史FAIL保留。
见[修复交付](SA15_I01_REPAIR_20261114.md)。整卡保持IN_PROGRESS，等待独立复验，SA16不启动，真实门NOT_RUN。

SA15 45a79cf/e940b27的I01独立原失败1PASS已核对并获用户接受，原110／89及独立原4／新3通过不重跑。
[独立关闭](accept/SAV1-15/independent-closure-20261114.json)。按用户指令启动SA16静默自主生命周期，
不将SourceDecision或正文当授权，继续复用原Engine/Mandate/共享预算/Assignment；共享范围另行精确协调。
SA17未启动，H12由另一CLI负责，全部真实能力门仍NOT_RUN。

SA16第41节91c0b75已核对，正式0048六表、五文件接线与13个Owner接口已落地。
已实现原Mandate/Run静默委托、冷却/去重/TTL/停止/等待/新Run重验、持久定时游标与恢复；
受控原图/Verifier、隔离Coding Worker及三处真实SIGKILL检查有分版本证据。
[SA16工程交付](SA16_ACCEPTANCE_20261114.md)／[机器摘要](accept/SAV1-16/accept.json)／
[接口与回退](SA16_AUTONOMY.md)。原失败定向修复，已通过项未重跑；最终本地门以摘要为准，
独立验收NOT_RUN，整卡IN_PROGRESS，SA17不启动。H12另一CLI负责，全部真实能力门NOT_RUN。

SA16-I01独立P1未放行：同话题已有任务时，冷却后create导致Thread唯一键冲突并回滚cycle。
当前私有修复6a8b467仅原same_rule失败函数1PASS/exit0，原探针字节/断言未改，新增重复claim
持久化而Thread/Ticket/Run保持各1。原独立24／跨规则1及93正式／87准备／静态通过未重跑。
[修复交付](SA16_I01_REPAIR_20261115.md)／[机器记录](accept/SAV1-16/i01-repair.json)。
整卡IN_PROGRESS，等待独立复验，SA17不启动；H12另一CLI负责，全部真实门NOT_RUN。

SA16 6a8b467／3eb08db的I01独立原失败1PASS已核对并获用户接受，原25独立与93正式／87准备
通过及旧失败保留，不重跑。[独立关闭](accept/SAV1-16/independent-closure-20261115.json)。
按用户指令启动SA17多渠道Outbox与节制通知，审计原Action/Approval/Outbox/Dispatcher、
Source通知授权、当前接收ACL、quiet/额度与未知回执；共享范围另行精确协调。
SA18未启动，H12另一CLI负责，真实平台／付费／外发／发布／长期门NOT_RUN。

SA17本地受控工程见[交付](SA17_ACCEPTANCE_20261115.md)与[通知接口/恢复](SA17_NOTIFICATIONS.md)；
100正式＋29准备分版本通过，未重跑已通过项。候选c478f6f，等待独立；SA18未启动，全部真实门NOT_RUN。

SA17-I01独立P2后私有修复f455c8e，仅原失败项1PASS，见[修复报告](SA17_I01_REPAIR_20261115.md)。
原49独立及其他历史通过不重跑，整卡仍待独立复验；SA18未启动，真实门NOT_RUN。

SA17 f455c8e／12e7547的I01独立原失败1PASS/exit0已核对并获用户接受；原49及历史129/静态通过不重跑，旧45外层错误保留。
[独立关闭](accept/SAV1-17/independent-closure-20261115.json)。按用户指令启动SA18 Review/精确审批/Draft PR，审计固定H08及原Action/Outbox/Review路径；共享范围另行精确确认。
SA19未启动，真实平台/付费/外发/发布/长期门NOT_RUN；H12由另一CLI负责，本副本固定真实门FAIL不变。
