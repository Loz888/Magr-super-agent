# SA17 多渠道 Outbox 与节制通知

当前在受控本地工程范围实施；具体候选、分版本成绩和独立状态见本卡 accept.json。

通知继续使用原 Engine 持锁 PG 连接、Action、Approval、Outbox 和 Dispatcher。五张0049表只存配置版本、因果/授权事实、Delivery观测与无正文事件，不拥有另一套任务、发送状态机或余额。接收、执行、通知和发布分别授权。通知发送/对账/恢复均不把原任务变为发布成功，也不覆盖原任务故障原因。

服务构造、API注册和默认设置均不启动 Channel 或调度器。服务端只能显式注册 `source-notification-channel-v1` fixture profile；真实 GitHub/飞书发送适配器和实际平台发送许可未配置。环境开关、安装SDK、ExecutionGrant、Router建议和Worker正文均不能打开通知。H12真实Agent/MVP在本副本仍保留FAIL，另一CLI负责其验收；SA18未启动。

## 通知事实与披露

仅四个固定模板：verified_result、necessary_question、review_available、critical_fault。程序从原Run及Verifier事实产生cause/hash；普通工具进度、模型自报完成和自由通知正文均不被接纳。

- 调查结果须原report_hash对应的通过verdict，且Run已达到可报告状态。
- Coding结果须当前epoch的最新attempt已result_recorded、精确可信验证passed；它本身不完成业务Task。原Coding Run可能仍是running，通知只说明已有核验记录/产物，SA18才负责具体Review Bundle和发布审批。
- 必要问题须原waiting_input；重要故障须真实failed，或明确预算/unknown原因的paused。
- 内容仅固定提示与Task UUID，要求到已认证控制台查看。无原文、日志、代码、自由URL或可复用凭据，data_class固定public_summary，仍须该类别的独立通知许可。private_log/private_summary许可不隐含public_summary。

首片只向确切Source安装和原SourceThreadRef发送；不开放跨来源转发。SourceScope、目的地、发送者、模板、实际body/payload hash、规则/配置版本、原Input及Task/Run链均持久绑定。ChannelMessage携带原话题、Action、body_ref和平台幂等键；清晰正文仅在发送调用处短暂出现，且只含上述最小模板。

## 授权与发送边界

Owner先保存明确NotificationGrant，再签署通知配置：当前policy revision/hash、服务端profile hash、原话题、内容类别、频率、cooldown、聚合和有效期。SourceInstance与Channel能力声明必须和已注册profile一致。配置CAS递增并保留签署历史；默认disabled。

计划时重验当前Source/canonical Input/身份角色/Receive/策略/任务来源链和真实结果，程序为实际模板payload_hash写入原Approval，随后创建原Action/Outbox。按项目、Run、cause、目的地和内容类生成持久幂等键；重连、重启、配置版本变化都不能重建同一结果的发送。

领取前重验当前授权、hash、epoch/revision、期限和quiet/频率/cooldown。quiet或限频只延期原ready记录；聚合窗口内更新cause会取消旧未发动作，保留旧不可变intent并只留一个最新待发动作。发送前先提交SA07通知配额，再开一个原事务做最终当前检查，提交后才调用Channel。已完成提交的撤权/角色变更在该边界会阻止实际发送。最后检查之后已经开始的远端调用按真实结果记账，不承诺撤回已发请求。

0049触发器随来源配置、policy、身份/角色、正文失效及通知配置更新，在同一原事务取消未发动作。过期、事实变化和原任务取消也由领取/维护检查阻断。在途与unknown保留，已消耗通知配额不伪退款。

## 回执与恢复

Dispatcher每次只发送一个已领取的原Action，匹配安装/scope、幂等键和外部ID才确认accepted；明确拒绝为failed，超时/丢回执/错scope或错键均为outcome_unknown。unknown永不进入ready，反复tick不重发。

支持查询的profile才调用只读query；not_found不证明没有发送，查询异常也保留unknown。不支持/未知查询能力时记录unqueryable并等待操作者，不提供把unknown随意标为未发的按钮。发送后撤权的晚到accepted回执只结算Delivery，不复活任务或改业务结果。

启动恢复保留原Action/Outbox unknown与最小Delivery事实。通知unknown跳过旧业务Run的OUTCOME_UNKNOWN原因更新；旧issue_comment/draft_pr继续原恢复语义。两个真实SIGKILL目标覆盖提交领取后、受控远端持久接受后。受控远端不是实际平台。

自身消息识别要求已知accepted回执、精确外部entity ID、Source安装/原话题和已验证机器人身份一致；正文标记或fixture链接不产生身份。未知回执和人类复制文本不算已确认的自身结果。

## Owner接口与维护

统一前缀 `/api/v3/source-notifications`。当前Owner cookie必需；所有写操作重验Origin/CSRF，严格有界JSON，响应no-store。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | /capabilities | 禁发默认值、已注册受控profile、未实测门 |
| GET / PUT | /sources/{source_id} | 读取/签署CAS配置 |
| POST / GET | /plans | 依据原Run/Input和内容类别计划/查看 |
| GET | /plans/{action_id} | 状态、hash和真实Delivery观测 |
| POST | /plans/{action_id}/cancel | 仅以精确payload hash取消未发动作 |
| POST | /tick | 有界调用原Dispatcher，缺profile不发送 |
| POST | /plans/{action_id}/reconcile | 仅unknown的能力约束只读对账 |
| POST | /maintain | 有界取消过期/撤权/事实过时消息及延期quiet |
| GET | /events | 有界无正文审计 |

主库不在本轮迁移或激活。停止通知用当前Owner将配置enabled=false或撤销Source policy；原pending取消，dispatching/unknown继续真实对账。保留0049及历史，不删除表或改旧迁移来回退。需要真实平台适配器、凭据/安装/目的地和具体外发许可时另行评审与独立实测。

## 所有权与证据范围

固定确认a09aef760e4c43ff46595a3c3cc56c503b018e97第42节覆盖0049五表、三文件接线及11接口；原SQL/补丁和完整哈希不改。初始接线后仅对Engine/API导入排序，非导入AST与符号集合相同。随后两个SIGKILL失败定位到原recover对通知也覆盖业务原因，精确同范围修复另存recovery-isolation-repair.patch与post-integration-repairs.json，不增加文件/接口/编号/预算范围。旧文件、失败、原探针和分版本归档完整保留。

已通过项按用户要求不重跑；版本汇总不等于最新候选单轮全量通过。静态成绩绑定其列明版本，后续修复按失败范围执行。独立验收尚未完成；真实平台、付费、外发、发布、全系统安全与长期运行均NOT_RUN。
