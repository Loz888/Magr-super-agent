# SAV1-07：项目总预算、配额与费用归集

状态：DONE（本地工程）。用户已接受SA07-I01原两项独立复验，并授权SA08。
本卡依赖 SA03；共享所有权按 Harness 固定 cb422c5e050eb48ad2cb0eae901453c052b5f018
的 SUPER_AGENT_HANDOFF.md 第30节实施，0036 归 Codex，0037 留给 Harness。
修复源码`6a5d089`的1161项及lint/typecheck为历史实施方证据；本轮仅原edit/create两项独立复验2PASS，用户已接受关闭。原FAIL与修复报告保留，见[独立关闭记录](accept/SAV1-07/i01-independent-closure-20261112.json)。SA08现已获授权。

## 权威与范围

原 Engine、同一 advisory lock、互斥锁与 PG 连接是唯一写者。预算子服务使用原事务；
不执行网络调用，不开启定时器、来源监听或模型。原 usage_calls/router_calls 是费用真值；
新表只保存签署预算、调用归属/预占元数据与配额事实，不复制模型结果或覆盖历史费用。
未配置新预算的旧入口保持原 grant/定价/结算行为；新 Source model_call 仍要求实际预算，
没有预算时明确拒绝，Router 语义评估未运行。正数计划额不产生付款或数据外发许可。

覆盖当前三条预占入口：Engine.reserve_call、RouterBudget.reserve、
RepositoryAnalysisStore.reserve_repository_call（包括验收模型）。三处原结算仍是唯一费用
结算入口。新预算通过原调用 ID 关联，所有检查与原预占在同事务提交/回滚。
未集成的 Harness PersistentBudgetLedger 不在本卡假装接线；后续必须按稳定契约另验。

## 冻结的预算契约

组织和项目分别保存带 revision、owner、UTC、review_ref、内容 hash 的配置及不可变历史。
项目绑定组织不可静默换绑；API 只管理本服务器的项目及其组织。配置时区为 IANA 时区；
金额统一 USD micros，拒绝 bool、负数、浮点数与溢出。初始各金额/Token/通知配额为0，
disabled，不启用真实付款；全部能力门通过后最多2个活动 Worker 的初始约束保持。

每个配置包含 lifetime/hour/day 金额与 Token 上限、Router 日额、Run 金额/Token 子上限，
候选小时量、队列占位、活动槽位、来源分钟速率与通知小时配额。Run 子额只增加约束，
不创建独立可累加的钱包。活动类型为 router/worker/verification/recall/subtask。
预占冻结模型、price_version、输入/输出单价及 Token 上界，必须匹配已签署价格清单。

调用归属表不保存第二份实际结算金额。用量视图从原账本联结计算：reserved/unknown
按完整预占占款；known 按原 settled_micros。Token 收据缺失时保留未知 Token 占用，
不能把零费用或缺 usage 当零 Token。历史未归属调用同样占共同余额；时间或 Token
无法可靠重建时保守计入当前窗口/阻止新增，不因新配置建立而清空历史支出。

窗口为明确 UTC 半开区间：本地小时保留 DST fold，本地日按下一个本地午夜结束。
所有未结/未知金额与 Token 无论何时创建，均带入每个当前窗口。新 Run、模式、
policy revision、价格配置、小时或午夜都不退还 unknown。真实晚到已知结算通过原
入口记账，释放一次对应预占；不恢复任务、不退款调用次数、不盲重发。

配额按组织与项目共同限制。幂等键绑定项目、类别、来源及 units；相同操作只占一次，
不同参数报冲突。候选/来源/通知采用窗口计量；queue/slot 是持久占位，只有明确完成或
取消事实允许释放。配额端口是服务端内部端口，不是让来源正文或模型伪造资源身份。
本卡提供配额服务及现有可用入口的接线；实际 Scheduler/Harness/通知发送能力仍按后续卡
开放，不把配额事实当已创建 Worker 或已发送消息。

## 管理与证据

新增 /api/v3/project-budgets 管理及用量视图，复用控制台 cookie、Origin/CSRF、事务内
当前会话复查与 no-store；不开放预占、结算、配额消费或付款 HTTP 入口。
只保存最小 ID/价格/费用/Token/固定诊断，不保存正文、密钥或供应商响应。

正式回归覆盖三入口共享限额、真实 PG 并发/回滚/恢复、跨 Run/mode/policy、午夜/DST、
unknown 结转/晚到结算、Token 缺失、历史旧账、组织/项目范围、配额幂等/释放和管理鉴权。
0035→0036 升级保持旧35迁移字节与费用行；旧升级测试仅机械追加36，不弱化原断言。
固定源码后已执行 lint、typecheck、真实 SAV1-07 注册检查；保留全部失败、日志及清理盘点。
本卡工程交付后等待独立验收；真实平台、付费调用、外发、发布、完整安全与长期门另列。


## 当前实现的补充约束

历史usage为非对象JSON时按缺失Token收据保守占款；旧仓库账本的正数token_reservation仍提供可靠上界，未知上界继续阻止新增。时间字符串必须是显式带时区ISO时间，不接受数字字符串epoch。SourceGateway限流拒绝不ACK，已验证的旧正文失效仍提交；Engine原Action校验（审批和领取时）检查当前共享窗口，Source通知许可仍独立，相同Action/窗口只占一次。没有Source绑定的旧issue_comment同样计入共享通知配额，Draft PR不占通知额度。


## 管理接口使用

| 方法 | 路径 | 内容 |
| --- | --- | --- |
| GET | /api/v3/project-budgets | 本服务器当前项目配置、费用/Token、配额用量及语义评估状态 |
| GET | /api/v3/project-budgets/{scope_type}/{scope_id} | 当前项目或其组织的相同视图 |
| PUT | /api/v3/project-budgets/{scope_type}/{scope_id} | expected_revision、definition、review_ref；原子保存签署新版本 |

scope_type仅organization/project。先保存组织，再保存本服务器项目且指定已有organization_id；项目组织不可换绑。创建expected_revision=0，修订使用当前revision，409时重新读取，不能覆写并发新版本。所有接口需控制台当前cookie，PUT另需可信Origin及X-CSRF-Token；响应no-store。没有源身份或模型可调用的签署/付款入口。

definition包含enabled、organization_id、IANA timezone、显式带时区ISO valid_from/valid_until、prices、caps、quotas。caps字段为total_micros/hour_micros/day_micros/router_day_micros/run_micros及total_tokens/hour_tokens/day_tokens/run_tokens；quotas为candidates_hour/queue/slots/source_minute/notifications_hour。金额USD micros、Token及配额只收非负整数，拒绝bool/字符串/浮点；slots最多2。prices逐项保存model_id/price_version及每百万输入/输出Token的USD micros单价。默认disabled/零额度，启用任何正数预算上限需显式价格；签署仍不代替原模型许可。
