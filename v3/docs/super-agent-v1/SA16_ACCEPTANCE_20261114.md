# SA16 本地受控工程交付（2026-11-14）

SA16本地受控工程门通过，整卡保持IN_PROGRESS，等待独立验收；SA17未启动。

- 源码候选：`becef2bacb423c69cf850317ce64b34ed4c7ccb8`。
- 源码指纹：`cd31d5ad147fb4432f0f1ffcd5db851207100d441462e55687d84886a13db9c8`。
- 共享确认：Harness `91c0b75693037bb0d90cb2eccd35356cd2e96135` 第41节。
- 正式0048六表、五文件原连接接线、13个Owner接口；45份迁移及479条Source产物引用一致。
- 93个正式目标、87个准备目标均有分版本PASS证据，剩余失败0，已通过用例重跑0。
  不声称最终源码单轮93/180全量通过，未重跑SA00–15或已经通过的静态目标。

## 实际交付

已冻结团队规则在原Engine持锁连接内转换当前已提交的would候选，创建原Mandate、
Thread/Ticket/Run及原Scheduler队列；规则/策略/Source/角色/profile/预算/仓库再次校验。
无关讨论和Shadow不产生Worker、回复或费用；默认无规则/profile/后台监听/周期模型。

冷却、去重、累计尝试数、主动任务TTL、语义进展、停止/等待、新Run重验与模式变更
均持久化。重开沿同一Ticket/Thread新建Run和Mandate，要求新的当前Source/决定与Owner
核验，不重置旧账本。定时固定时区/日历、持久日期游标、DST和有界合并，默认仅信号。

真实PG＋原只读图/Verifier已生成内部报告且无Action/Outbox。受控fixture_git任务经原
隔离Coding/Broker实际修改代码、运行测试和计入原共享用量；通过验证后等审，不伪称业务
完成。预算预占提交后关闭规则的精确边界阻止了模型发送，原预占保留。

三处实际SIGKILL已通过：准入提交前回滚、提交后重启去重、关闭规则提交后的持久停止。
只读状态视图来自原Run/verdict/CodingVerification、用量及资源回执；展示等待、失败、
预算/unknown和待审状态。维护优先处理实际活动任务；原取消同时回收未派发队列配额，
未知费用/进程不释放、不重试。文件/网络/Docker IO留在业务事务外。

## 分版本检验

| 轮次 | 候选 | 执行项 | PASS | FAIL |
| --- | --- | ---: | ---: | ---: |
| private-initial | be88c95 | 77 | 77 | 0 |
| draft-initial | 3d97eb2 | 10 | 10 | 0 |
| formal-initial | eec5b53 | 64 | 44 | 20 |
| formal-repair-01 | 62da397 | 20 | 18 | 2 |
| formal-repair-02 | 3ebe2bb | 2 | 2 | 0 |
| new-boundaries-initial | 32c5463 | 18 | 15 | 3 |
| new-boundaries-repair-01 | a882167 | 3 | 3 | 0 |
| final-boundaries-initial | 9bf8e5c | 11 | 10 | 1 |
| final-boundaries-repair-01 | becef2b | 1 | 1 | 0 |

每轮在测试前先提交并校验不可变Git归档/源码指纹，前后指纹一致。历史FAIL及测试准备
错误全部保留：最初ambient寻址夹具/错误码断言、合并窗口、受控Router预算及unknown
注入位置、静态类型/格式和产物目录依赖哈希；只复验各轮剩余失败。

原五文件补丁先按确认字节落地。随后Scheduler仅为lint换行私有方法签名，AST完全
相同；精确补丁、before/after SHA和对应首次新检查保留，见
[格式修复链](sa16-proposal/post-integration-format.json)。未增加接口、共享文件或预算语义。

lint/typecheck按目标和版本分别保留。最终Source Python AST与9bf8e5c一致；之后只修
字符串换行与文档/配置哈希，不重跑已通过的全仓静态目标。私有初轮runner的scope里
有旧Recall标签，真实argv/JUnit均明确为SA16契约77项；原字段保留，不作为SA15成绩。

## 范围与未运行门

工程PASS只限本地受控Source、单一当前输入、原只读路径和fixture_git。原生GitHub资源
缺原winner factory、真实模型门未过或可信执行profile缺失时明确等待。不扩大旧只读
许可，不引入平行Task/余额，不把Context/模型文字当控制授权。完整Review Bundle/发布
和常驻部署分别属于后续SA18/SA20。没有默认激活规则或后台模型调用。

H12由另一CLI验收，本副本固定真实Agent/MVP FAIL保留；未导入H12提交。真实平台、
付费、外发、发布、全系统安全与长期运行均NOT_RUN。受控模型3个已知假费用回执及一笔
发送前保留预占只存在隔离测试PG，不是真实供应商调用/费用。

本卡8个隔离PG容器、测试数据库/会话及8个原资源provider身份均已核对无容器残留；
原用户文件、.env和历史失败保留。未修改或部署业务主库，仅在隔离PG安装验证正式迁移。

机器摘要：[accept.json](accept/SAV1-16/accept.json)；
完整性审计：[artifact-audit.json](accept/SAV1-16/artifact-audit.json)；
独立交接：[independent-request.txt](accept/SAV1-16/independent-request.txt)；
操作/回退：[SA16_AUTONOMY.md](SA16_AUTONOMY.md)。

原始证据根目录：
`evidence/super-agent-v1/SAV1-16/start-20261114T125101Z-8e48a364/`。
其manifest.json绑定所有轮次归档、日志、JUnit与摘要；注册轮的原始结果另位于
`evidence/SAV1-16/`、`evidence/SAV1-16-NEW-BOUNDARIES/`及
`evidence/SAV1-16-FINAL-BOUNDARIES/`，逐例绝对路径/哈希见机器摘要。
