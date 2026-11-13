# SA12 本地工程交付，等待独立验收

候选 `6b48b7d6d5d4f60719a9d99928589bb019169024`，源码 SHA256 `9171d59578c14b8c726cc884794666f8c7a57de2b2d3b1ab8fc97bf8bdf346dc`。SA12 保持 IN_PROGRESS；SA13 未启动。

Scheduler、Session、Assignment、资源租约、队列及容量视图已接到原 Engine 和15个正式接口，复用原Task/Run/Mandate与SA07配额。固定H03工作区及H04/H06隔离Broker已在本副本真实验证。默认无配置、Worker、driver或监督线程；执行仅为明确的fixture资源试验，真实Coding桥与写grant留SA13。

第37节确认 `298d4c5c5123a5c26c34a9673d676516d1503f84` 的0044与接线已落地。旧41SQL不变，主库原入口只安装0044，当前42checksum一致，七张新表均0行。0045仍归Harness，不导入H12/0037/0040。Engine/API/contracts/H03与批准字节匹配；Broker两处行长折行的AST相同，原补丁和修复差异均保留。

| 阶段 | 绑定候选 | 实际成绩 |
| --- | --- | --- |
| 首次正式SAV1-12（252新卡＋169直接依赖） | 158fb51 / a41ac8b2047f1075… | 416 PASS、5 FAIL |
| 仅原5项失败复验 | 6b48b7d / 9171d59578c14b8c… | 5 PASS、0 FAIL，exit0 |

五项原失败均发生在测试最终缺失探针：未格式化inspect对不存在容器输出`[]`，导致“stdout为空”断言失败。修复只给该探针增加format，并加强return1和精确容器名错误校验；原断言及生产代码不改。没有将`[]`、daemon错误或未知结果放宽为absence。实际停止、slot释放及两个“create后撤权/跨期限，实际start为0”断言在旧日志中也完整保留。未重跑416通过项，不宣称最新源码单轮421PASS。

首轮静态11处E501／1处Core类型流失败保留。158fb51仅原7个lint文件及原1个strict-mypy目标通过，其他通过目标和后续fixture修复后均未重复；一次未指定项目配置的mypy诊断不计正式通过。

验证含真实PG事务回滚/竞争、原epoch只推进一次、实际签名canonical CI队列与人类公平、当前Source撤权/编辑、旧Session/lease/epoch拒绝、unknown及原read_jobs保槽、可信静默后等待释放、两个实际H03目录、三处原Engine进程SIGKILL及一处心跳Worker真实SIGKILL后实际容器停止。受控无进程文件driver与真实Docker用例分开，不把前者当真实进程证明。

最终临时PG／会话／SA12容器均无残留，主库usage/router/shared-budget调用行均0。12份清单248引用匹配，修复候选469个生产文件与首轮候选逐字节一致；保护用户文件及SA11独立26份历史不改。完整系统安全、语义、真实来源、付费、真实编码、外发、发布及长期门NOT_RUN。

[机器摘要](accept/SAV1-12/accept.json) · [接口与边界](SA12_SCHEDULER.md) · [首次原始FAIL](../../evidence/SAV1-12/20261113T081623Z-c347df46/result.json) · [原5项复验](../../evidence/super-agent-v1/SAV1-12/start-20261113T061235Z-20197e1e/20261113T083328Z-original5-failed-only-c64025/commands.json) · [安装／清理盘点](../../evidence/super-agent-v1/SAV1-12/start-20261113T061235Z-20197e1e/final-inventory.json)
