# SA16 静默自主生命周期：待协调共享范围

SA15 候选 45a79cf / 交付 e940b27 已获用户接受独立复验，原失败1PASS；只启动SA16。
SA17不启动。H12由另一CLI验收，本副本固定H12真实Agent/MVP FAIL保留；真实平台、
付费、外发、发布、长期运行均NOT_RUN。本范围仅受控本地工程。

SA16使用原Engine的持锁连接、事务与mutex；不建立第二Task/Run/费用账本。持久新增
六张委托/版本/信号认领/定时游标/原Run引用/审计表，任务状态读取原Run、Job和回执。
规则是当前Owner保存的精确版本与review hash；SourceDecision仍是无执行权的would提案。
只在重新核对当前Source/身份角色/策略/规则/仓库/配置/预算/资源及可信服务端profile后，
用原Mandate/Thread/Ticket/Run创建入口及原Scheduler排队。

0048只提议在SA副本使用；不占用Harness编号，不导入H12，不改历史迁移。SQL只增加
六表和guard、current委托谓词，并以CREATE OR REPLACE保留sa_coding_current原OID与
全部旧条件后追加委托fence。原操作known回执/结算条件不改；未知费用/进程仍占原预留。

精确五文件补丁：Engine组合与恢复hook；API注册；SourcePolicies在原工具检查前追加
委托检查；SourceCoding的Owner入口保留，将同事务准入体提取为受信私有方法，并允许
仅有效团队规则委托替代explicit_request；SourceScheduler同样提取原队列准入体。
这些私有方法不挂HTTP，必须原连接和INTRANS，调用方先验Owner签署规则与全部当前事实。
未绑定SA16的旧Run/只读许可不升级。没有文件/网络/Docker IO进入SQL事务。

两个模式使用不同原执行路径。auto_investigate创建明确read_only原调查Run，既有只读
ToolAuthority/SourcePolicy继续限权；auto_repair仅复用已验收fixture_git Coding桥、隔离
Worker与Broker。首切片不扩展CodingGrant为只读、不增加真实profile/付费入口，也不让
context_only机器人变请求者。GitHub原生资源缺原winner factory、真实门未过或可信profile
未安装时明确等待；不以伪Issue或新平行任务绕过。

规则修订/关闭推进版本，逐新操作SQL/Python重查立即拒绝旧委托；维护原cancel/epoch、
队列和assignment drain，晚到known回执仍如实结算。心跳不当语义进展；无进展/TTL结束
停止，等待可安全释放确认资源，未知资源不释放。重开需新Owner核验、当前新输入/决定、
epoch/规则/profile/预算，不重置旧账本，不由机器人/任意消息自动重开。

定时默认不开工；明确时区/日历/有效期/allow_scheduled_work/当前seed Source引用与执行
配置才可产生获准任务。持久local_date游标、rule/date唯一键、DST fold0及不存在时刻跳过，
停机只合并最近一个有效tick；tick不伪造Source身份/新正文/模型结果。

13个当前Owner接口，前缀/api/v3/source-autonomy：

- GET /capabilities
- GET /rules
- PUT /rules/{rule_id}
- GET /rules/{rule_id}
- POST /rules/{rule_id}/disable
- GET /claims
- GET /attempts
- GET /attempts/{attempt_id}
- POST /attempts/{attempt_id}/stop
- POST /attempts/{attempt_id}/reopen
- POST /maintain
- POST /tick
- POST /cycle

API复用当前cookie/Origin/CSRF，严格有界DTO，不接受runtime/factory/provider/bearer、
模型自报权限/结果。只读视图显示原状态、委托依据、等待/预算/失败/审批和资源未知；
所有动作无通知/发布副作用。没有默认常驻调度器/监听器/周期模型调用。

验证计划：原连接PG事务、严格授权/签署/失效/乱序、重复/并发认领、预算不重置、
规则停用与最后派发边界、真实SIGKILL恢复、受控Coding原Worker路径、DST/时区游标、
Owner API与无副作用负例。先保留私有77PASS和草案首次成绩，不重跑历史通过项。
正式迁移与接线等待新独立确认；最终工程成绩与真实能力门分别记录。
