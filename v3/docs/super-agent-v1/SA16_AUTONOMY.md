# SA16 静默自主任务生命周期

状态：IN_PROGRESS，工程实现及分版本检查见 SA16_ACCEPTANCE_20261114.md。独立验收NOT_RUN，真实能力未放行。

SA15 I01 独立闭环已核对，见 accept/SAV1-15/independent-closure-20261114.json。
SA16共享确认91c0b75693037bb0d90cb2eccd35356cd2e96135，第41节；正式0048与五文件
按绑定的SQL/补丁字节落地，确认见 sa16-proposal/ownership-confirmed.json。

## 规则与任务依据

服务构造不做IO、不起线程、调度器、监听、模型或通知。默认没有规则及可信profile。
Owner以当前cookie/Origin/CSRF保存精确SourcePolicy版本、rule_ref/hash、服务端profile、
目标hash、仓库、有效期、冷却/并发/尝试次数及TTL。正文和would决定不产生授权。
profile只可由受信Python端口注册，不能由HTTP/model传入；相同hash内容不可变。

cycle读取已经提交的SA09 would决定，重验原快照、当前Source/角色/正文版本、规则、
策略、仓库/路径及共享预算，再创建带原Mandate的Thread/Ticket/Run与原Scheduler队列。
服务不调用新任务模型，也不直接执行工具。忽略/观察/提问不启动Worker或回复。
Shadow只记录事实；切自动模式需SourcePolicy与本规则新签署，旧决定不升级。

auto_investigate复用原read_only调查Run，禁edit_files、run_tests、model_call、network和
发布。它的精确SourceRunPolicy继续参加原ToolAuthority检查。auto_repair复用原SA13
fixture_git原Coding/Worker/Broker，不开放H12真实Agent/MVP失败范围。任务实际状态、
已知验证结果、失败和待审来自原Run、verdict或CodingVerification，SA16不自行宣称完成。

初切片只接单一当前Source输入的受控profile。多输入组合、原生GitHub资源缺原winner
factory、无执行器、真实模型门未过均显示具体等待原因；没有伪Issue或平行任务表。
Source别名、关系和模型候选不绕过原精确资源归属或当前授权。

## 停止、等待与重开

规则修订/停用推进持久revision，SQL与Python逐操作检查旧委托立即失效；当前事务
传播cancel/epoch、关闭未派发队列、原Coding fence与Assignment drain。之后的维护
不代替每次最终派发的授权检查。关闭规则不关闭独立Source接收，也不发通知。

Owner可按attempt revision停止；任意群成员、context_only机器人或Source正文不拥有
此控制入口。无语义进展到idle TTL或到任务有效期会停止；心跳、轮询和模型自报进展
不延长期限。waiting_input/waiting_approval/paused可进入原drain流程，只有原资源回执
确认后才释放。实际进程、unknown资源/外发/模型费用保留原记录和预占，不重试/退款。

重开须新的Owner review、新Source/决定、当前Rule、原epoch、profile、余额及资源已静止。
沿同一Ticket/Thread新建Run和Mandate，不复活旧Run，不重建旧总预算；冷却和累计次数
跨新Run及规则revision计算。只读及旧显式Mandate不会因为保存新规则获得写权限。

## 定时、恢复与观察

显式DailySignal固定IANA时区、HH:MM和星期，默认allow_scheduled_work=false；tick只
留无正文信号，不发模型。明确允许工作还须审阅seed Source引用及当前规则/profile。
rule/date唯一键与持久local_date游标跨revision保留；DST重复小时用fold0，不存在时刻
跳过；停机最多合并最近一个有效tick，不无限补跑。过期seed不冒充新输入。

Engine恢复hook只从原PG重核既有活动委托、Run/Job/费用和资源，不周期调用模型，
不把unknown当失败重发。SA20常驻调度、真实服务部署及72小时运行另卡验收。

13个接口见 sa16-proposal/SA16_SCOPE.md。rules/claims/attempts均为有界、当前Owner、
no-store元数据视图；attempt显示原Run/outcome/reason/report_hash、真实验证引用、
unknown费用、未确认资源、待维护原因及原assignment状态。静默不隐藏等待和失败。
所有接口禁注入runtime/provider/factory/bearer/自报成功；无通知和发布副作用。

回退：当前Owner先disable规则，检查新revision与旧任务fence，再使用原Scheduler/
Coding资源核对流程清理；unknown留待对账。保留前向迁移与历史，不删六张审计表，
不修改历史迁移或账本。没有新增0049编号预留。

## 分门记录

私有77项在be88c95首次PASS；隔离PG草案10项在3d97eb2首次PASS，未重跑。
正式检查及修复分别保存不可变候选、归档指纹、命令/JUnit和原始失败；修复只重跑
失败范围，不把分版本证据说成最终源码单轮全量通过。

SA17未启动；H12由另一CLI负责，本副本固定真实Agent/MVP FAIL保留。真实平台、
付费、外发、发布、全系统安全及长期运行门NOT_RUN，受控fixture不能提升这些门。
