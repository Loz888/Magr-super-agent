# SA11 本地工程交付（2026-11-13）

SA11 动态订阅、Source 引用 Mailbox 与交回已具备独立验收条件。候选 `ca49b760c2439147bb4555a335c975b69a878dbb`，
源码指纹 `f94ed73b645a6c06514b3c1f5811c9889f74467026150236ae04232313403f2e`。本卡仍 IN_PROGRESS，独立验收 NOT_RUN；SA12 未启动。

主人审核的明确资源、原生话题或 reply 链绑定原 Task/Run/Mandate。新 canonical 输入
在原 Engine 同一持锁事务内投递引用，省略新任务语义评估；每条输入仍重验当前
Receive、身份、角色、策略、正文 head/TTL、目标及 Mandate。primary/context_only/
observer 分 lane，分页最多16条/64KiB；准备和ACK各自持久化，游标CAS与epoch拒绝
旧消费。正文仅经当前授权后内存物化，observer不能物化Worker正文，持久事实不存
Source正文，不复制旧 question 或 graph checkpoint。

queued 当前 epoch 可由主人准备/确认引用元数据，回执不表示指令或工具已应用。
原 begin_run 提升epoch后旧页失效；paused、终态及旧epoch继续拒绝页面消费。重复
prepare仅在参数、输入水位、完整当前引用/订阅头/consumer和期限一致时复用。不同
Run必须显式rebind到原Ticket内已存在的获准attempt，不自动恢复或新建Run。

handback追加一次needs_routing/loop_blocked无正文待办，保留原决定、unknown及预算，
不直接调用Eval。Scheduler、实际Worker和安全点接线属于后续卡。

## 固定协调与正式集成

Harness `d0af0747` 第35节确认0042、七表、11个主人接口及四文件最小接线；
`a9b02bfe` 第36节确认0043最小前向函数修复。0042原字节/checksum保留，0043仅将
跨表record的CASE改为独立IF，拒绝谓词相同；两份迁移均由原入口安装。当前41份
checksum一致，七表默认空，未导入0037/0040或H12。原SourceEval/I01发送栅栏、财务
方法、H02和用户文件保持。

## 逐版本验证与失败保留

| 候选 | 范围 | 成绩 |
| --- | --- | --- |
| fe8ecac | 原注册351项首次 | 309 PASS / 42 FAIL |
| 4615aa1 | 仅原42失败；0043 | 2 PASS / 40 FAIL |
| 4ef3e18 | 仅剩40失败；私有UUID规范化/prepare幂等 | 14 PASS / 26 FAIL |
| 8b5fc1f | 仅剩26失败；queued引用状态门 | 25 PASS / 1 FAIL |
| ca49b76 | 仅最后1失败；显式读取权限的正例夹具 | 1 PASS |

最后用例原夹具未授予Receive.model_input，生产正确拒绝。仅该失败用例在正常主人
策略创建前显式设置model_input=True，原断言全部保留，并增加只返回第二有效输入
及其精确正文的断言；生产权限门、旧helper及其他用例不改。每轮源码前后一致；
并发、外层事务回滚、三处真实SIGKILL恢复等各有对应版本通过证据。

原309及中途通过项没有重复执行。原42最终按2+14+25+1各自取得真实PASS，不能写成
最终源码单轮351PASS或42PASS。最初静态失败仅按3个lint文件和1个Core类型目标修复
通过，绑定b80e0cc；后续未重复通过的静态目标，不冒称最终全库静态复跑。

- [原注册FAIL/JUnit](../../evidence/SAV1-11/20261113T045454Z-6ade8d74/result.json)
- [逐轮源码、命令、JUnit、SHA与边界](accept/SAV1-11/accept.json)
- [最终只读保全与清理核对](../../evidence/super-agent-v1/SAV1-11/final-integrity-20261113T052537Z-082a20/final-integrity-and-cleanup.json)
- [协议与11接口说明](SA11_SUBSCRIPTIONS.md)

原40SQL、批准四文件、全部历史FAIL、SA10独立记录及保护用户文件均保留；11份清单
204引用匹配。临时PG/会话、本卡故障容器与本副本检查进程0，未操作相邻Harness活动
树。本卡工程门PASS仅指上述受控工程和分版本证据，完整系统安全、独立验收、语义、
真实平台、付费、外发、发布与长期门仍NOT_RUN。
