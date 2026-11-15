# SA17 本地受控工程交付（2026-11-15）

SA17本地受控工程完成，等待独立验收，整卡保持IN_PROGRESS；SA18未启动。
源码候选 `c478f6f62e1aaadd332592c52e9c99fe76b5c3ce`，官方源码指纹 `d3c6bfb907e250db803a1ccd7c61f6e950619f78283339ca1abec7ec57060673`。运行逻辑最后修复83ad632，之后仅新增/更新schema、合成禁发样本、产物清单和首次完整性目标。

原Engine/Action/Approval/Outbox/Dispatcher已接入独立通知Delivery；0049五表、11个Owner接口和GitHub/飞书受控Channel可用。通知严格绑定原话题、当前授权、真实结果与实际模板hash；普通工具进度不发送，原文/日志/代码/URL不进入模板。通知配额提交后再做最终权限读取。unknown只读对账、不重发；回执与恢复不覆盖业务任务状态/故障原因。详细语义、接口与回退见[运行说明](SA17_NOTIFICATIONS.md)。

固定Harness确认a09aef7第42节已经只读核对。原SQL/三文件补丁均保持确认字节；Engine/API只排序导入，非导入AST与符号集合相同。真实SIGKILL后发现原recover也给通知Run写OUTCOME_UNKNOWN，已在相同Engine所有权和通知恢复隔离范围内修复；补丁、before/after与原因见sa17-proposal/post-integration-repairs.json。无新共享文件/接口/编号/预算语义，也未导入H12。

## 分版本验证

| 范围 | 不可变候选 | 本轮结果 |
| --- | --- | --- |
| 首次契约/模板/quiet/profile/隔离PG草案 | 43e2df5 | 29 PASS；lint/typecheck PASS |
| 首次正式原Engine/Dispatcher/API/PG/恢复 | 240bec2 | 83项：43 PASS、40 FAIL；新静态目标FAIL |
| 仅原40失败的修复准备 | 1e755d4 | 路径选择错误，0执行、exit4保留；失败静态目标PASS |
| 仅原40失败 | 1e755d4 | 38 PASS、2 FAIL；原43及静态未重跑 |
| 仅原2 SIGKILL失败 | 260061d | 2 FAIL；已实际触发两个SIGKILL，暴露业务原因污染 |
| 首次新增边界 | 260061d | 12项：7 PASS、5 FAIL；新测试lint FAIL |
| 仅剩余原2和新增5失败 | 83ad632 | 7 PASS；仅失败的新测试lint PASS |
| 首次schema/禁发样本/清单/迁移与补丁来源/登记核对 | c478f6f | make verify TASK=SAV1-17-CATALOGUE：5 PASS；新测试lint PASS |

100个正式独特目标（95运行＋5完整性）及29准备目标均有各自通过证据；没有重复任何已通过目标，不称最终候选单轮129或100全量PASS。每轮在检查前提交、归档并验证官方源码指纹，检查前后指纹稳定。首次40失败均由受控安装升级后策略仍引用旧Source revision造成，修复测试配置，没有放宽产品校验。两次SIGKILL早期未到故障点的失败、0执行选择错误和其他历史红灯全部保留。

新增Coding审查探针最初错误期待原Run在result_recorded后已waiting_approval；既有Coding契约保留running。本轮保存原失败，改为断言真实running及不发布/不完成；服务根据当前epoch的最新final attempt和可信passed验证显示可供审查产物，SA18仍负责正式Review Bundle/精确发布审批。

静态成绩绑定其列明版本；后续恢复/私有验证事实修复按用户要求未重复已通过静态目标，不能声称最终候选全量lint/typecheck重跑。正式初始hash守卫的元数据期望因同范围恢复补丁更新，原已通过目标未重跑；新完整性目标核对了当前补丁及全部引用。

## 完整性、清理与限制

46份正式迁移、16份Source清单和514条产物引用匹配。七个本轮临时PG实例均已停止删除，各自测试库清空，无本卡会话残留；受控Coding测试按原Assignment journal清理并断言容器移除。原用户文件SHA f4adf960…d34a29f保持未改且仍未跟踪。主PG 55435仍44份迁移，0048/0049未安装，本轮只读核对，未改变主库或启用后台发送。

真实平台、付费、真实编码能力、外发、发布、全系统安全与长期运行均NOT_RUN；本副本固定H12真实Agent/MVP FAIL保留，H12由另一CLI负责。当前只支持服务端明确注册的fixture Channel，受控回执、实际隔离容器测试及只读恢复不能冒充真实平台/Agent/发布验收。

[机器摘要](accept/SAV1-17/accept.json) · [完整证据与逐目标版本映射](../../evidence/super-agent-v1/SAV1-17/start-20261115T041117Z-02d2ff43/delivery-integrity-audit.json)。

复现100个当前正式目标的注册入口为 `make verify TASK=SAV1-17`；本轮依用户要求只执行首次与失败范围，没有在最后整套重复。29准备目标的草案前置绑定归档43e2df5，不在已正式安装0049的当前树重跑。
