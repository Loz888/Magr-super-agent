> 当前SA08已由用户工程放行：原3项独立PASS，新增权限/撤权/别名/无副作用6 PASS；未重跑历史全量。见[关闭记录](accept/SAV1-08/independent-closure-20261112.json)。下方为历史候选交付记录。

# SA08 本地接线与影子观察交接

状态：IN_PROGRESS，工程交付完成、独立验收NOT_RUN。当前修复候选为
`9950ebd4a59127199ee00e7e407d5bc50f61619d`，源码指纹`69ddeb1cce88bba4841907ae467743d563c0691d2c812a24f011e9fa6d447f27`。
首轮c7214a7完整注册1358 PASS；本地SA08-L01原3 FAIL确认后仅定向3 PASS，未重复已通过项。
当前注册1361项，修复后全量NOT_RUN。详见[正式摘要](accept/SAV1-08/accept.json)。
本文件仅本地准备，未新发消息或声称Harness已接收实现。

## 已确认的共享范围

用户代转Harness固定提交`1e397ab73feeaa6e9a5d747963bb9889eec5a51f`的
SUPER_AGENT_HANDOFF.md第31节。该提交仅增加交接文档35行；SA侧已只读核验固定原文、
确认范围及本地SQL/接线提案hash，实际确认记录为
`evidence/super-agent-v1/SAV1-08/start-20261112T081641Z-5eb84f0a/ownership-confirmation.json`。

Codex在SA副本独占0038_super_agent_fast_path.sql、app.sa_fast_path_*和
super_agent/fast_path*.py；共享变化仅为Engine.__init__组合FastPath子服务、api.py注册
/api/v3/source-fast-path，以及SourceIngress对本次receipt新建canonical input的观察hook。
Harness已明确不并行修改这些方法、路由、hook或SA活动树。H12环境修复不在本卡导入。

确认全文SHA256为`036090e77a01340c3e4652a470442ee8ae21c2020fe1204ac66db95d6abf4dc5`。
SQL与接线补丁分别匹配已确认的
`7f03acdf8e428a0cb621ab555d3b04d44f7dc76127f4e5eedf0c23a698466eb6`及
`3269bc6c33ba572c9057a345bba0c1c73119be772686a2be7af1ecb0dc1422f7`。
此前工具缺失、用户代转前未获确认的阶段记录均保留，实际确认另行归档。

## 本地实现边界

原Engine mutex、advisory lock、持锁PG连接及事务继续是唯一业务写者。新canonical输入只
保存一次无正文origin；观察时重新校验当前来源、身份、策略、Receive、head、TTL、删除、
歧义和历史/缺口状态。已有别名不改写origin；没有可信origin的旧canonical不推断为live。
观察容量不足保留pending，既有入站的必要失效和durable ACK/回滚语义保持。

规则仅记录拒绝理由、would_*假设及有界话题batch。控制观察也只记录would_pause/cancel，
仍需当前真实身份、角色和精确Ticket授权。本卡不创建或执行Ticket/Run/Worker，不调用
模型，不发送通知或发布。SA09接入前Router语义评估始终NOT_RUN，管理员策略模式不会
由正文、环境变量或影子结果改变。

每个新普通batch复用既有SharedBudgets.quota的candidate入口，重投及同batch新增成员
不重复占用。预算/结算算法、ToolAuthority、SourceEvent DTO和Harness实现均未改；
PersistentBudgetLedger与SA共同预算的后续接线仍需单独契约和复验，本卡不推定已经覆盖。

0038已落入SA迁移目录；本树共有37份迁移，即0001–0036及0038。0037继续归Harness，
不导入、不改名、不伪造占位，旧迁移字节保持。本卡不重命名或集成Harness历史0030。

## 已注册接口与验证状态

`/api/v3/source-fast-path`已注册六个管理接口：来源设置PUT/GET、observe POST，及
preview、decisions、metrics GET。复用当前控制台cookie、写操作Origin/CSRF、事务内
会话复查和no-store；observe仅接收已有input ID，不接受正文或调用者权限断言。
字段、容量与规则见[SA08说明](SA08_FAST_PATH.md)。

首轮197项新检查和原1161项共1358 PASS/0 failure/error/skip。之后本地发现富文本at元素
使合法控制降为普通候选；原2解析/Adapter+1真实PG探针均失败。固定占位符修复保留全部
现行权限检查，迁入正式注册的原3项断言定向3 PASS，函数AST未改。当前新增200项/总1361，
修复后未重复全量或初轮已过lint/typecheck。三项失败、分版本指纹与清理盘点均保留。

SA08工程交付等待独立验收；SA09未启动。真实平台、付费、外发、发布与
长期门保持NOT_RUN。固定第31节只确认所有权，不改变任何SA/H卡验收或真实实验许可。
