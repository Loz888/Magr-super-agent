# Super-Agent / Harness 协作约定

依据：用户于 2026-11-10 指定 Codex 负责 Super-Agent，本地 Trae 负责 Harness，
并允许按需调用 Trae 协作。

## 所有权

| 工作 | 负责人 | 工作目录 |
| --- | --- | --- |
| SAV1-00–SAV1-24：来源、身份、策略、Router、订阅、调度、Recall、通知与运维 | Codex | `/home/imini/magr-super-agent` |
| HV4-H00–HV4-H18：模型/工具/工作区/Broker/Verifier、编码循环、上下文与恢复 | Trae | `/home/imini/magr-harness` |

双方在每卡实施前完整阅读对应任务书与验收文件，并保存阅读时间与文件指纹。
源码与业务事实继续由原 Engine/PostgreSQL 协议约束；分工不会新增业务写者。

## 当前交接

Trae 仓库已提交 H00，并声明其工程/安全门通过；真实模型、Agent 和发布门未运行。
本副本已只读定位 H00 accept.json，尚未导入或复验该提交。H01 仍是 SA00 的未完成前置。
本副本分工前的局部 H00 静态清理保留，不再独立推进 Harness 整卡。

## 向 Harness 请求的 H01 交付

1. 已提交的稳定 commit、选型 ADR、接口 schema/版本、状态所有权映射和验收证据路径。
2. ModelAdapter、ToolRegistry、WorkspaceProvider、ExecutionBroker、ContextManager、
   Verifier、EventSink 的正式契约；哪些是冻结接口、哪些仍为建议需分别说明。
3. Source/策略生成的 Mandate 如何映射为 Task/Run，包含 actor/scope、repository/base SHA、
   workspace、input revision、execution epoch、预算预占/结算和 unknown 结果语义。
4. 持久 inbox/安全点、命令 Job 的启动/查询/取消、恢复与旧 Worker fencing 的能力边界。
5. Diff/tree、验证回执及 Review Bundle 的版本绑定；受控实现与真实能力门分别记录。

这些是 Super-Agent 的依赖需求，不预先指定或实现另一套 Harness。H01 未完成时只做
SA00 审计与需求准备，不宣称依赖满足或开放真实自动修复。

## 变更与集成

- 不直接修改另一方的活动工作树。交付用具体 commit、差异和证据，避免复制未提交文件。
- 共享 Engine/API/预算/领域对象变更先明确唯一实现负责人；迁移编号按集成时最新编号协调。
- 接口改变需说明版本、受影响 SA/HV4 卡、兼容方法及必要回归；不悄悄改变完成/授权语义。
- 在本副本审查与解决冲突，使用自己的数据库/Redis/临时目录执行验证后记录集成结果。
- 对话用于确认任务、接口和阻塞；稳定约定写回仓库。消息送达、对方确认、代码完成和
  本地集成复验是不同状态，分别记录。
- 外部写、付费模型、机器人接入与真实渠道实验仍按原任务书的具体授权及验收处理。


## 已接收依赖与下一步（2026-11-10）

H00 固定交付 de4775e 已集成。H01 v1.1 固定交付 38a65ba（实现 418d0c2）在本副本
merge 782f35c 后，独立原始权限探针 2 项、H01 注册 18 项、真实 PG 回归 5 项和
全库 lint/typecheck 通过。旧 e1b3214 两项权限 FAIL、原核心回归终态竞态 FAIL 均保留。
SAV1-00 冻结提交 6aeb1b0，其注册 120 项通过，正式验收见 accept/SAV1-00/accept.json。

已向 Trae 当前对话发送接收结论并要求按任务书继续 H02；首次消息的接收已由交接
文档确认，本次接收结论的后续确认以 Trae 新交接记录为准。消息待发送不视为已送达；
若需在任务执行期间立即纠正，使用待发送消息中的“直接发送”并确认处理结果。
Sources 0030 已在 H01 交接确认归 Codex，尚未创建；双方 ADR 使用轨道内命名空间。
共享 Engine/store 的终态原子性修复已明确协调由 Trae 负责并纳入此固定交付，
Super-Agent 不同时编辑这些文件。下一卡 SAV1-01 仍须完整重读任务书及对应要求后实施。


## SAV1-01 完成与交接

实施提交 8dc641bbdf1fe7d0f057e11ec66e951f2874f2f8，验收提交 5fda544。
source-contract-v1/source-event-v1 与 0030_source_contracts.sql 已落地，注册验收
79 项通过；源码指纹绑定 schema 和合成样本。接口与能力边界见 SA01_CONTRACTS.md，
给 Harness 的完整交接见 HARNESS_SA01_HANDOFF.md。新来源身份不直接转成 Harness actor。

Trae 正在执行 H02 修复；非紧急 SA01 交接已排队，UI 明示“等待发送”，当前未收到
Trae 确认。没有使用“直接发送”打断其检查。来源/执行/平台监听仍禁用，H02 的活动
文件未由本轨道编辑。下一卡 SAV1-02 的共享迁移编号需先另行对齐。


## SAV1-02 实际确认与固定交付

SA01 交接已由 Trae 固定提交 6109ce5 §12 确认；上文等待发送为当时状态，历史保留。SA02 的 0031 迁移与新身份管理 API 路径已于 2026-11-10T14:50:28Z 获 Trae 实际确认，固定提交 0ca76063ab91177cd7255c475435d3d6b00c726c §13，凭据见 SA02 起始阅读证据。

本卡实施 3bcfb0234789e000ed26597bc8a6af378675791d，注册 132 项 PASS，具体说明 HARNESS_SA02_HANDOFF.md。没有编辑/导入 Harness 活动文件，也没有将 H02 变成已验收。新能力未启用真实平台/模型/外发。SA03 仍 PLANNED，0032 未预占。此轮完成交接消息尚未发送/确认，不能把文档准备当送达。


## SAV1-03 实际所有权确认与交付

SA02 已由 Trae 在固定 4ad86c1 §15 实际收讫；上文 SA02 完成消息未发送为历史状态，保留。0032/source-policies API/受限 Engine 接线实际确认 4ad86c1 §15，ToolAuthority._grant pre-charge 接线实际确认 f61a233 §16；两份确认与 UTC 记录在 SA03 起始证据。

SA03 实施 9f64dc9000879ae84a71640a6d96bdb4668b332e，注册 304 项通过，见 HARNESS_SA03_HANDOFF.md 与验收摘要。没有修改/导入 Harness 活动文件，不改变其 H03/H04 状态。SA03 完成消息未发送/未确认，文件准备不能当送达。等待独立复验，不自动进入 SA04，0033 未预占；真实来源、模型、外发/发布保持未启用。

## SAV1-05 实际所有权确认

用户已确认SA04独立复验通过，并授权SA05。Trae在固定79798690c93497d1740d54cc91fc2f755f55f250第24节实际确认0034、sources/github*、GitHub管理注册/旧Webhook单owner分流归Codex。确认已只读核对，凭据见SA05起始证据；不是排队消息推断。Engine实际未改，预算/ToolAuthority/Harness未改或导入。H系列仍由Trae负责，本轮只核对上述固定交接，不运行其检查或改变其验收状态。

## SAV1-06 实际所有权与固定本地候选

SA05 用户独立工程放行候选 9fb6593（实现 cf7c176）已核对原始566/146/13记录和
产物指纹，原失败不改。用户转达 Trae 已实际确认0035及飞书专用文件/API所有权，
本树只读核对固定 d0053f4d7bdf850f4c3c458bd36d3c10b9a3e650 §29。不是把排队消息
当确认；最初 UI 发送未成功的记录保留，不重试此前失败的 UI 恢复。

SA06 本地源码候选852a96d543a68b14fa2da86234d4be44921a783f，默认 Runtime=None、
无监听/凭据登记/外发。原Engine/ToolAuthority/预算/Harness未改；原来源Ingress只增加
当前角色/kind的context_only判定，不伪造历史标记。旧GitHub Webhook整体AST和0001–0034
SHA原样；所有业务事实仍走同一Engine持锁PG连接。0035已在本副本loopback:55435应用，
35份安装校验和全匹配。固定候选完整注册结果另见SA06验收，不提前记独立PASS。

本轮无Harness活动树改动/导入或H卡推进。完成交接尚未再次发送/收到对方确认；文件
准备不代表送达。0036未预占。SA06完成后等待独立复验，不自行启动SA07；真实渠道、
模型、编码、外发/发布和长期门保持NOT_RUN。

## SA06 健康跟进与最终本地工程结果

初候选852a96d的729PASS保留；之后健康独立1FAIL/正式3FAIL阻止交付，仅Runtime.status
修复新鲜、当前配置/模式负面事实投影，不复用旧granted/online。原断言16PASS后固定
15591aea214e7a83f3bf5ad112bda85b4294664e，完整注册744PASS，lint/typecheck229filesPASS，
前后源码指纹2f47b76904cff3fb2e1c062258364e1a495a310150e4a759d42bfc16772ee100一致，28项hash匹配。
详见SA06_ACCEPTANCE_20261111.md、HARNESS_SA06_HANDOFF.md和accept/SAV1-06/accept.json。
交接仅本地准备，未声称新的消息送达/对方接收。停在SA06等独立复验，不进入SA07；
0036仍未预占；真实来源/模型/外发/发布/完整系统安全/长期门NOT_RUN。

## SA06 三类独立失败修复候选（仍未独立通过）

源码d8004baadbf8e0afff1fc9e2dbbcd82ee2cf93ba，指纹4738e6421c3e4648e269e3720aec18c42cb5ad93528501eba5bf00f07a91a863。
只改Codex私有Feishu模块/现有审计事实和回归；无新迁移/共享Engine/API/schema/预算或
Harness活动树操作。旧独立744/10TMPDIR失败、4FAIL及格式5PASS4FAIL保持原状。
本次单轮795PASS/exit0，lint/typecheck231PASS；格式原探针9PASS/exit0，四项原探针
3PASS/1FAIL/exit1（无可信native proof在mint前安全拒绝，原探针未捕获）。不猜造证明
或修改探针强求绿，本卡仍NOT_PASSED，等待新独立验收，不进入SA07。未向Trae发送
新的完成消息或声称送达；0036仍未预占，真实平台/付费/外发/发布/长期门NOT_RUN。

## SA06 独立关闭与 SA07 协调启动

用户已接受dfab5c3的SA06-I04定向独立2PASS，并允许推进SA07；SA06工程关闭记录见accept/SAV1-06/i04-independent-closure-20261111.json。历史FAIL和原用户文件不改。

SA07需要项目/组织共享预算、配额与原Engine预占/结算接线。已向当前Harness CLI维护任务发出新接口范围确认请求，原Trae UI恢复未重试；发送回执不是对方实际确认。拟议0036_super_agent_budgets.sql、app.sa_budget_*、/api/v3/project-budgets、新super_agent/budget*及Engine/Router预算最小接线均待固定提交确认；0036仍未预占。本树先准备契约与验收矩阵，不修改对方活动树或推进H卡。

SA07后续实际确认：Harness固定cb422c5e050eb48ad2cb0eae901453c052b5f018第30节已核对原文及SHA，0036及上述范围现归Codex，并包括RepositoryAnalysisStore.reserve_repository_call最小同事务hook。本卡不导入/编辑Harness；其0037及PersistentBudgetLedger集成留待后续固定契约复验，不继承H12付费授权。原发送/待确认段落保留为历史状态。凭据见本卡起始evidence的ownership-confirmation.json。


## SA07 工程固定交付（2026-11-12，待独立验收）

实现6fd95819883ed923d963e5d89858917a2fa3eab8，完整注册1118PASS，lint/typecheck234filesPASS；证据见accept/SAV1-07/accept.json和SA07_ACCEPTANCE_20261112.md。0036已应用本副本，旧35迁移不改；只读复核cb422c5第30节，未编辑/导入Harness。实际PersistentBudgetLedger/0037后续集成仍单独验收。SA07保持IN_PROGRESS，独立验收未运行，SA08不启动。交接仅本地准备，不声称新的消息送达或对方接收。


## SA07-I01 本地修复候选（等待独立复验）

原候选独立P1已原样确认2FAIL；修复6a5d089仅改私有SourceIngress、新增43项正式回归及当前清单。固定完整注册1161PASS，原两项修复方回放与lint/typecheck通过；原独立FAIL不改，SA07不放行、SA08未启动。见SA07_I01_REPAIR_20261112.md。无新迁移/共享Engine/API/预算或Harness活动树改动，0037仍归Harness；本轮交接仅本地准备，未发送新的消息或声称对方收讫。


## SA07独立关闭与SA08启动（历史启动记录）

用户接受固定6a5d089的SA07-I01原两项独立2PASS并授权SA08。原1161项实现方结果与历史FAIL均保留；复验只重跑原未通过项。SA08正在核对来源私有Fast Path/Shadow、共享Engine/API与迁移编号；0037已由Harness使用，未在本树占用新编号或改动其活动树。


## SA08共享范围实际确认与本地接线（历史接线阶段）

用户代转Harness固定提交`1e397ab73feeaa6e9a5d747963bb9889eec5a51f`，已只读核对
SUPER_AGENT_HANDOFF.md第31节及SQL/接线提案指纹。该提交仅增加交接文档35行，明确确认
0038_super_agent_fast_path.sql、app.sa_fast_path_*、新super_agent/fast_path*模块，及SA副本
Engine.__init__组合、api.py注册/api/v3/source-fast-path、SourceIngress本次新canonical输入
观察hook归SA侧Codex；Harness不并行修改这些接线。实际确认记录见本卡主证据根的
ownership-confirmation.json；此前等待确认和未找到回复的记录均保留其历史时点。

本地固定提交`c7214a7cf9615a8f8f8381094348feb1c34a874a`已应用上述接线与0038迁移，
六个cookie管理接口已注册。本树迁移目录为37份，即0001–0036及0038；0037继续归Harness，
不导入、不改名、不伪造占位。原Engine同一持锁PG连接和事务保持，既有candidate配额端口
仅被复用，历史迁移、预算/结算算法、ToolAuthority、SourceEvent DTO与Harness实现均未改。

当时首次整合lint/typecheck PASS，首次完整注册1358项（SA08新增197项及原1161项）正在运行，
结果待原评分器生成；最终结果及修复见下节。SA08保持IN_PROGRESS，独立验收NOT_RUN，SA09未启动；真实平台、
付费、外发、发布与长期门NOT_RUN。新[SA08交接说明](HARNESS_SA08_HANDOFF.md)仅本地准备，
未新发完成消息或声称Harness接收了本卡实现；固定第31节确认也不构成本卡验收。


## SA08工程交付，等待独立验收（2026-11-12）

共享接线/0038固定c7214a7，首轮原注册1358 PASS，指纹37a618…0898b一致。随后本地发现
SA08-L01：飞书post前置at使合法控制退化，原3探针全部失败后，仅私有规则固定占位符修复
为9950ebd；原断言迁正式注册定向3 PASS，当前指纹69ddeb…47f27一致。当前注册1361项，
依用户要求未重复已通过项、完整注册或静态整库检查；历史通过与修复定向证据不混称单轮。
见SA08_ACCEPTANCE_20261112.md和accept/SAV1-08/accept.json。37份安装checksum匹配、
临时PG/会话/故障容器无残留，用户原文件未变。SA08仍IN_PROGRESS待独立，SA09未启动。
HARNESS_SA08_HANDOFF.md仅本地准备，未新发消息/声称实现收讫；不改Harness或其真实门。


## SA08独立关闭与SA09前置核对

用户已接受9950ebd的独立原3项及新增6项PASS，初轮1358绑定核对通过且未重复。
SA08本地工程DONE，历史失败/用户文件保留，真实平台/付费/外发/发布/长期门NOT_RUN。
当前SA09开始H02固定交付及旧Router复用审计。已重读Harness1e397ab第31节，
仅确认SA08/0038/shared FastPath；不扩大到SA09，不导入H12。若SA09需要新共享
迁移/Worker/预算钩子，先形成具体草案再协调，当前不声称已有其确认。


## SA09实际所有权、H02固定集成与首轮准备

用户转达Harness固定0e0adc6a5bb5a7e0c3a3c8323e33513e4b911d84第32节，新0039及
Router/Worker/Engine/API范围均已只读核对SQL8b188405、补丁73b28025和请求db67ee69
完整hash。旧风险元数据的02206b85已保留归档并修正，不把第31节SA08确认当作SA09。
0039已安装本副本，四文件接线按批准patch应用；保留原37 SQL字节，当前38份无0037。
H02仅固定169925f六blob，正式接收fee0875，首次H00/H01/H02兼容58PASS；不导入H12、
0037或对方活动树，不继承真实实验许可。Source只用H02公开SSE/能力helper，不造Run。
私有协议、真实PG/恢复与API组件通过记录及原失败均保留，完整1777注册正在准备冻结，
当前不先记整卡PASS。200合成数据仅待操作者复核草案，真实语义与其余门NOT_RUN。


## SA09本地工程交付，等待独立验收

首轮固定e9ab11e/180c9b…19417，原1777注册1776PASS/1FAIL，0ERROR/SKIP；旧SA06 future
负例受跨秒影响，经测试时钟隔离后源码93e9a9d/684fe9…36ce仅原失败1PASS。生产源码、
原断言、评分器、数据、SQL和schema不变，旧FAIL保留，不称修复后完整1777PASS。
lint首轮PASS；typecheck初轮245文件仅domain两处失败后只该文件PASS，未重复通过目标。
143引用、38安装checksum、旧37SQL和三结算AST匹配；临时PG/会话/故障容器0，主库六新表0行。
200例合成标注仍待操作者复核，语义和所有真实/长期门NOT_RUN；工程记录见
SA09_ACCEPTANCE_20261112.md和accept/SAV1-09/accept.json。SA09保持IN_PROGRESS等待独立，
SA10未启动。HARNESS_SA09_HANDOFF.md仅本地准备，未发送新的完成消息或声称对方验收。


## SA09-I01定向修复与用户授权SA10推进

原93e9a9d独立NOT_PASSED/P1在预占提交后缺当前发送栅栏，三组原6FAIL已复现保留。
仅私有SourceEval/Model修复ee1fee3/76fc815b…7a6fa，原6项定向6PASS，未重跑通过项或静态/整卡。
已提交新6项AST相同镜像供后续注册，当前1783没有全跑；真实/语义/长期门NOT_RUN。
144引用、38安装checksum、固定H02及旧SQL/财务AST匹配，无临时PG/会话/故障容器。
见SA09_I01_REPAIR_20261112.md，原独立和用户文件不改，修复候选待独立复验。
用户明确“后续如果成功修改了错误，通过了检验就继续进行SA10的任务。”原6失败复验通过已满足，
据此推进SA10需求/所有权核对，不伪造独立PASS。Harness固定f554937第33节新增0040归Harness，
SA10不能占0040；0039/第32节确认不扩大到SA10，新的共享范围和拟0041仍待具体协调。


## SA10 私有组件准备与新范围待确认（2026-11-13）

SA09-I01原6失败定向6PASS满足用户继续SA10的条件，未把原独立NOT_PASSED改为PASS。SA10私有实现、SQL草案和精确最小共享patch均已备齐，见[具体请求](sa10-proposal/ownership-request.txt)。已请求用户代转，目前没有新的固定确认；Harness最新固定f32bad605c57bffa481b8a69379f19a482420978的交接文档仍止第33节，0040归Harness，SA09第32节不覆盖本卡。消息请求、固定确认、实现接线和验收状态继续分别记录。

拟0041草案SHA d6aead811237e532664adf62b15521a16369fd29b8dfe9d26156f0d74fec1ee3；两文件共享patch SHA 352277eb82d61b75534a072c6ece1c6769c4653102bac1399e36d021e419c278。确认前正式migrations、主库和Engine/API不变，无H12、0037、0040或Harness活动文件导入。组件成绩见[准备摘要](accept/SAV1-10/preparation-20261113.json)，正式201项仅收集未执行；独立与全部真实门NOT_RUN。


## SA10实际确认与正式集成修复（2026-11-13）

用户转达并只读核对Harness固定0a335a14d8ce37703b5cfe0eaa7960d4ac571fc7第34节，SQL d6aead81、补丁352277eb、请求61e7f271的完整SHA均匹配。确认后在SA副本按原补丁组合SourceCorrelations并注册六个管理接口，0041正式落地及原入口安装；原38SQL不变、当前39，无0037/0040或H12导入，没有操作对方活动树。上方“等待共享确认”保留为历史时点。

SA10正式191ed23原201轮200PASS/1FAIL；只修剩余接线守卫对无path FastAPI对象的枚举，生产源码和原断言不改，dcea520仅原失败1PASS。见[工程交付](SA10_ACCEPTANCE_20261113.md)及[交接说明](HARNESS_SA10_HANDOFF.md)。本卡本地工程完成、当前候选待独立复验，SA11不推进；全部真实/语义/长期门NOT_RUN。新完成交接文档仅本地准备，未声称消息已发出或Harness已收讫本次实现。

SA09依赖元数据也同步既有真实独立6PASS：原ee1fee3/76fc815b候选三组发送前撤权负例全部零POST，见[关闭记录](accept/SAV1-09/i01-independent-closure-20261113.json)。本轮未新跑其独立流水线；原独立FAIL、原始日志和旧摘要字节保留。正式201中SA09依赖35项属于本卡用户要求的首次正式集成范围，不能冒称新的独立闭环。


## SA10独立关闭与SA11授权启动（2026-11-13）

用户接受SA10 dcea520的原2失败守卫独立2PASS、39checksum/原连接/六路由和199历史首次证据绑定，见[关闭记录](accept/SAV1-10/i01-independent-closure-20261113.json)。未重跑通过项、抹去旧FAIL或放宽真实门。本轮据用户明确指令启动SA11，先完整阅读/记录fingerprint及Mailbox/Source权威复用审计。

第34节仅确认SA10/0041；SA11新增订阅/Source投递与消费协议、Ingress分流和API需新具体所有权/接口/迁移编号协调。当前只读最新Harness固定交接仍止第34节，0042为候选未预占；不编辑/导入对方活动树、H12或旧0037/0040。先备齐新SQL与精确patch再请求固定确认，当前不把阶段准备当接线或验收通过。


## SA11 固定确认落地、首轮失败与前向修复准备（2026-11-13）

用户回传并只读核对Harness固定d0af0747dd129033cb7bd0abd2af9ca5a57a8d4a第35节，0042 SQL与四文件补丁完整哈希一致。确认后仅在SA副本落地对应正式迁移与接线，当前40份迁移checksum一致、七新表无数据；未导入H12/0037/0040或对方活动文件。

fe8ecac首轮351项309PASS/42FAIL已完整保留。36项直接PG错误及6项同保存路径API503定位到0042共用canonical_guard的CASE同时解析不同record字段。不能重写已安装0042，已准备最小0043前向函数修复（SHA256 55f7e50886149eca9ae78db2bc5e72d186b41679fc403c8c318aa6fb8e10a008），不变原拒绝谓词、表/API/授权。新编号具体请求见[转发文件](sa11-proposal/canonical-guard-repair-request.txt)，已请求用户代转；当前尚无新固定确认，0043未安装，不把第35节扩为新迁移编号确认。

确认后只复验原42失败，309通过不重复。只读完整性/清理核对与失败阶段摘要分别保留，不能当作工程或独立通过。SA11仍IN_PROGRESS，SA12及真实/付费/外发/发布/长期门均未启动。


## SA11 第36节实际确认及分版本工程交付（2026-11-13）

用户回传并只读核对Harness固定a9b02bfe946f643d58b868ad3bcb4ad851aeec5e第36节，SQL55f7e508完整哈希一致，确认提交仅交接文档。随后SA用原迁移入口安装0043，仅前向修复原canonical_guard函数，原40SQL保持，主库当前41。上方等待确认为历史时点，不代表当前状态。

原351首轮309PASS/42FAIL后，仅42→40→26→1失败范围逐轮实跑，新增PASS分别2/14/25/1；原始红灯及各版本绑定均保留。最后候选ca49b76/f94ed73b，本地工程已具备独立验收条件，见SA11_ACCEPTANCE_20261113.md；未宣称最新351单轮PASS或重跑通过的静态目标。四文件补丁不改、I01/H02/财务/历史用户文件保留，11清单204引用、41checksum及无残留核对通过。

未改/导入Harness活动树或H12，没有发送新的实现完成消息或声称Harness认证本次实现；本节仅SA本地工程状态记录。SA11保持IN_PROGRESS，独立验收NOT_RUN；SA12不启动，真实、付费、外发、发布与长期门不变。


## SA11 独立关闭与SA12授权启动（2026-11-13）

用户已接受SA11 ca49b76的原42独立42PASS及六有效边界、41checksum/204refs，无序版本错误预期探针两轮FAIL仍保留；关闭记录见accept/SAV1-11/independent-closure-20261113.json。原309未重跑，源码未改，真实/付费/外发/发布/长期门不变。

据用户明确指令启动SA12需求/复用/依赖核对。H03/H04必须审查固定交付及接口后在本副本验证；本地现有H01/H02端口不能冒充已导入工作区/隔离Broker。第35/36节只覆盖SA11，不扩大到Scheduler/Assignment/新迁移；先备齐SQL和精确共享patch，再请求固定确认，不直接编辑或导入对方活动树。


## SA12 第37节固定确认及正式接线（2026-11-13）

用户回传的固定确认298d4c5c5123a5c26c34a9673d676516d1503f84已逐项只读核对：仅协调文档，0044 SQL7199a802、三文件patch7899c89d、固定H03及H06绑定hook补丁ddc37d4e完整SHA一致。已只在SA副本按批准字节正式落地；不读取或导入对方活动文件，也不应用H12、0037或0040。0045_harness_github_delivery.sql仍留给Harness。

原Engine/API只按三文件补丁最小接线；H02 contracts仅兼容增加ExecutionRequest.execution_epoch=0，其原版本与清单保留，其他五个H02固定产物不变。私有Scheduler和资源adapter使用原PG连接、原Task/Run/Mandate与SA07配额。默认无driver/Session/监督线程，只有显式fixture资源试验；真实Coding桥仍留SA13。

当前真实PG、独立进程、H03工作区与隔离Broker测试已编写，首次正式检查尚未执行，不把schema安装草案检查或固定文件一致当运行验收。SA12仍IN_PROGRESS，独立与全部真实/语义/长期门NOT_RUN，SA13不启动。后续实际检查与清理结果另以分版本原始证据记录。


## SA12 分版本工程交付（2026-11-13）

第37节298d4c5c已落地。候选6b48b7d/9171d59578c14b8c…，正式0044原入口安装、42checksum/七表0行。首轮421为416PASS5FAIL；五项仅测试absence探针format不符，生产不改，仅原5复验5PASS。原416及静态已通过目标不重复，历史FAIL保留，不称最新单轮421PASS。两处Broker折行另有AST相同证明，批准原补丁不改；H02仅兼容epoch字段，其他五产物原字节。

见[交付](SA12_ACCEPTANCE_20261113.md)及[分版本摘要](accept/SAV1-12/accept.json)。完整性/清理盘点与运行成绩分别记录，本地工程完成、独立NOT_RUN；SA12仍IN_PROGRESS，SA13不启动，真实/付费/外发/发布/长期门不变。没有发送完成消息或声称Harness已认证本次实现。


## SA12 独立关闭与SA13授权启动（2026-11-13）

用户已接受SA12固定6b48b7d的独立原5PASS及新增4PASS；42checksum/248refs、源码9171d595和保护材料绑定一致。关闭前已保存旧当前摘要原字节，独立原文件、原421=416PASS5FAIL与所有历史红灯不改，原416及静态通过目标不重复。见[关闭记录](accept/SAV1-12/independent-closure-20261113.json)。

按用户明确指令启动SA13需求及固定H11/H12依赖审计。第37节仅覆盖SA12，H11旧任务创建/财务入口不能直接复制成第二Task/钱包，旧调查grant不能变成写许可。新增共享Engine/API/schema/预算范围须先准备具体提案并确认；0045仍归Harness，不读取或导入对方活动/未提交文件。真实能力与质量门各自核对，不把工程环境恢复或模型连通当真实Coding放行。


## SA13 具体提案及独立准备（2026-11-13）

提案提交b6079db已备齐：拟0046六表379e9de4、四文件patch01c7ec61、固定H11最小模块与兼容补丁、六个metadata接口；完整SHA见[提案清单](sa13-proposal/proposal-manifest.json)。已请求用户代转[具体文件](sa13-proposal/ownership-request.txt)，尚无新固定确认。第37节只覆盖SA12；正式迁移/Engine/API/Scheduler/contracts和固定内核导入均未应用。

私有139项、lint/typecheck262首次通过；隔离草案43迁移/六表安装与临时kernel预览另列，不把这些记整卡运行通过。H12真实Agent/MVP FAIL保留，只继续受控工程准备。主库仍42、0045仍归Harness，真实/付费/外发/发布/长期及SA14不启动，见[阶段摘要](accept/SAV1-13/preparation-20261113.json)。


## SA13独立关闭与SA14具体准备（2026-11-14）

SA13 f53f423／c49cf1f用户接受独立32PASS已核对关闭，19份证据复制核验；原212分版本及
早期两轮只有工作树指纹的限制保留，未重跑旧通过项。按用户明确指令启动SA14。
H09固定7d8f0ab／096300e复核及本副本H11 SafePoint一致，只复用既有端口，不导入文件inbox／第二Store。

SA14私有65首次PASS；隔离SQL草案29项分版本通过（原27通过不重跑，2setup错误定向2→1闭环），
lint定向修复PASS，首次typecheck275PASS未重跑。每轮先不可变提交并归档；原失败／两次归档checker
测试前中止记录保留。见[准备摘要](accept/SAV1-14/preparation-20261114.json)，不称最终单轮94PASS。

[具体请求](sa14-proposal/ownership-request.txt)／[完整提案hash](sa14-proposal/proposal-manifest.json)：
拟0047六表、Engine／API／Ingress三文件最小hook及12metadata接口，仅本SA副本归SA。
§38只覆盖SA13，等待新固定确认；不应用正式SQL／patch，不写Harness活动文件。
主库仍43、无新六表；临时本卡PG／会话清理，旧用户容器不动。实际服务／路由／异步Runtime／
PG-Docker并发恢复及正式注册未实施／验收，SA14保持IN_PROGRESS。H12真实Agent/MVP仍FAIL，
真实／付费／外发／发布／长期门NOT_RUN，SA15不启动。

## SA15第40节实际确认与本地工程交付

用户转达Harness固定e0432f0e289de868a5956339aeec76db71bd0efe第40节已确认；本副本只读核对该提交
唯一协调文档变更及全部提案hash，Engine/API两文件按152b61e7…33cad9精确补丁落地，after hash仍匹配。
不新增迁移或预占0048、不读取或导入H12活动实现；无新Graph/授权/正文库或业务写者。

SA15源码bc1f91c，本地受控110正式目标及89准备目标有分版本PASS；原3失败只3项复验，后续原catalogue
失败只1项复验，其余为此前未运行首次检查。静态与源码版本限制保留，不称最后候选全量复跑。
44checksum／443refs匹配；4个本轮PG和2个实际受控Job容器无残留，用户文件与历史失败保持。
[完成交接](HARNESS_SA15_HANDOFF.md)仅本地准备，未宣称新消息已发送或对方已再次收讫。
等待独立验收，整卡IN_PROGRESS，SA16不启动；真实/付费/外发/发布/全系统安全/长期门NOT_RUN，H12由另一CLI负责。

SA15-I01后续：独立发现默认搜索时窗漂移，已在第40节拥有的私有服务/内部加密游标内修复，
无共享Engine/API/schema/SQL/预算新范围。45a79cf只原失败1PASS；不重跑独立原4与新3通过或其他历史/静态通过。
原FAIL与用户材料保留；等待新的独立复验，SA16不启动，H12另一CLI负责。见SA15_I01_REPAIR_20261114.md。


## SA17 第42节固定确认及本地工程交付（2026-11-15）

固定a09aef760e4c43ff46595a3c3cc56c503b018e97只含交接文档，0049 SQL35467606和三文件patch289879fe完整hash核对一致，已仅在SA副本落地，11Owner接口不变。导入排序与原recover通知unknown业务原因隔离修复另有精确累积记录，均在已确认文件/通知恢复范围，无额外共享文件/接口/编号/财务语义；原SQL/补丁不改，无H12导入。

源码c478f6f；100正式＋29准备独特目标有分版本PASS，原已通过项未重跑，历史失败/0执行保留。46 repo迁移、514引用一致；主库只读仍44，0048/0049未安装。工程完成待独立，整卡IN_PROGRESS，SA18不启动，真实能力门NOT_RUN。HARNESS_SA17_HANDOFF.md仅本地准备，不声称另发完成消息或Harness认证实现。


## SA17-I01私有修复（2026-11-15）

独立P2来源为别名与实际Channel thread身份不一致。f455c8e只修复已归SA的私有通知模块，统一原生接收范围去重/聚合/限频并兼容旧intent，不改Engine/API/schema/迁移/共享预算，仍在第42节a09aef7已确认范围。仅原独立失败1PASS/exit0；原49PASS、历史129和静态不重跑，原45外层汇总KeyError限制保留。原探针/报告/失败/用户文件不改。等待独立复验，SA17未放行，SA18不启动；H12和全部真实门不变。详见SA17_I01_REPAIR_20261115.md。


## SA19 第45节确认与本地工程交付，2026-11-15

SA18独立关闭已获用户接受。SA19第45节8d19308确认已核对，正式0052及原Engine/API/首页三文件按精确hash落地。候选7c2d768，78正式独特用例分版本PASS、54准备PASS保留，修复只复验失败项/目标，不称最终同版本全量通过；见[本地工程交付](SA19_ACCEPTANCE_20261115.md)与[逐例摘要](accept/SAV1-19/accept.json)。49仓库迁移/584产物引用匹配，主库仍44，无本卡临时资源残留。SA19等待独立验收，SA20未启动，真实门NOT_RUN，固定H12真实Agent/MVP FAIL保留。

[具体确认](sa19-proposal/ownership-confirmed.json)绑定0052两表、三文件精确补丁和15接口。未读取或修改Harness活动树，未预占0053。私有资源防回退补丁没有改变共享三文件的确认后字节。静态检查分版本记录，旧FAIL保留。HARNESS_SA19_HANDOFF.md只是本地准备，不声称外发或Harness已验收实现。


## SA19-I01私有静态挂载修复，2026-11-15

SA19-I01独立P2（非空ASGI root_path绕过私有静态保护）已确认并本地定向修复。候选045934c仅原三个失败参数3PASS/exit0，HTML别名GET为404、JS/CSS HEAD为405，均no-store；原探针字节/断言未改，其他33独立通过、历史78正式/54准备与静态通过目标均未重跑。见[修复报告](SA19_I01_REPAIR_20261115.md)及[机器记录](accept/SAV1-19/i01-repair.json)。SA19保持IN_PROGRESS、未放行，等待独立复验；SA20未启动，主库仍44，真实门NOT_RUN，固定H12 FAIL保留。

只改SA私有source_console_api.py及其Source目录指纹，无共享Engine/API/首页/迁移/预算语义变化，不增加接口或新编号。未操作Harness活动树或H12；不要求新的共享所有权确认。


## SA20具体提案与阶段准备，2026-11-15

SA20当前：私有契约/纯信号/0053隔离草案70项与限权客户端18项均有分版本PASS，原失败仅定向复验、通过项不重跑；静态结果按版本保留。见[实施边界](SA20_OPERATIONS.md)／[70项准备](accept/SAV1-20/preparation-20261115.json)／[客户端摘要](accept/SAV1-20/client-preparation-20261115.json)。[0053四表/三文件/11接口具体请求](sa20-proposal/ownership-request.txt)待Harness新确认，正式SQL/共享接线未应用，部署与备份恢复演练NOT_RUN。SA21未启动，主库仍44，真实门NOT_RUN，固定H12 FAIL保留。

提案a9e2a7a/记录3f75b60，0053 SQL 1ae8bd6e…d2c37、三文件patch ecc71e2f…6c74a，完整哈希在请求中。私有客户端后续实现不改SQL/patch，已通过项未重跑。当前只读取固定Harness提交8d19308，未应用其活动树或声称已收到SA20新确认。
