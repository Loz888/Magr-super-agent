# SA09 来源 Router / Eval 本地交接

当前补充：原独立SA09-I01未通过，修复ee1fee3原6失败仅定向6PASS；原报告保留，修复候选待独立复验。详见[修复记录](SA09_I01_REPAIR_20261112.md)。用户授权修复检验成功后继续SA10，已开始前置；不将此记为独立PASS。下文为首次交付历史快照。

状态：IN_PROGRESS，本地工程交付完成，独立验收NOT_RUN。本文件仅本地准备，未向
Harness发送新的实现完成消息，不声称对方已接收实现或本卡验收通过。

首次冻结候选为`e9ab11e82920b62967568b967219d1b1fcc597ff`，原评分器源码指纹为
`180c9b684c03b72239b464a5fe2245f9edca692d1fdc882859394154b4519417`。
初轮1777项：1776 PASS、1 FAIL、0 ERROR/SKIP，原结果仍为FAIL。唯一旧SA06 future时间负例
经测试时钟隔离后仅定向1 PASS；生产源码和原断言不变，未重跑已通过项或静态通过目标。
最终源码提交`93e9a9d9a64bdd19889071ebf3ee33d3e46bb482`；指纹`684fe90a43065e642860ca3b99fbc932406951e92b3bd41392eb590e386436ce`。
工程交付使用上述跨版本证据，不称修复后完整1777 PASS。

## 已确认范围与实际接线

Harness固定`0e0adc6a5bb5a7e0c3a3c8323e33513e4b911d84`的SUPER_AGENT_HANDOFF第32节
确认SA侧Codex独占0039、app.sa_source_eval_*、私有Source Eval模块及明确的四文件接线。
这份确认只确认范围，不认证实现或工程/真实实验结果；SA08的1e397ab第31节不能代替本轮。
实际确认已只读核对，并保留原请求、SQL及接线补丁。

0039 SQL SHA256为`8b18840590c7841022d7985d66904cd580aee916777cadc9f8baca3cb1837669`，
四文件补丁SHA256为`73b28025f6b92d415aa2347c28e1c8959ee749c966f6e8d5e0a71ff88c445e1b`。
本副本已安装0039，SQL及实际接线与批准版本匹配。38份迁移为0001–0036、0038、0039；
0037仍归Harness，不导入、不改名或伪造占位。原迁移和三处财务结算AST保持。

共享变化限于旧Routing排除Source标记并拒绝跨管线批次、RouterWorker成对可选注入
pipeline/decoder、Engine组合及API注册。旧缺省流程与DTO保持。Source持久快照使用
source-router-snapshot-v1，原router_batches兼容外壳仅额外带threads=[]。

H02只按固定169925f实现和fee0875接收元数据选择性集成；Source模型仅复用公开SSEDecoder、
ModelCapabilities、estimate_tokens，使用独立请求，不伪造TaskBinding/Run/Workspace。
没有导入H12、其付费许可、0037或Harness活动树未提交内容。

## 调用、授权和执行边界

SourceEval默认disabled。HTTP只以已有FastPath批次ID排fixture队列，202表示持久排队、
semantic_evaluation=NOT_RUN；HTTP不接受正文、模型提案、模式或权限断言。live必须由受信
runtime显式提供mode和原RouterPermit，并注入client/credential；没有默认provider或监听。

Input/Source/identity/policy、签署预算、TTL、成员revision与精确目标epoch在发送前和提交时
重新核验。控制优先级100不送模型，attach目标还要求全部成员的准确controller关系及
当前follow_up Admission。ignore/observe/attach/create/ask仅形成would_*且不可执行的决定，
没有业务Ticket/Run/Worker、群发、通知或发布副作用。

费用使用原router_calls/authorization_budgets及SA07归属。已知费用与sanitized自有结果
在同一原Engine外层事务原子提交；原settle的result保持None。不调用旧response或get存取
新协议，原始正文/响应不进入普通CAS。预占须durable后才POST，拒绝caller外层savepoint；
unknown/reserved和known-invalid按canonical输入阻止借新配置、批次或成员revision重发。
原Engine.recover负责原金融恢复，ready决定和已知安全结果复用时不再次调用provider。

Source指标区分recorded_model_attempts、known_billed_calls、unconfirmed_model_attempts；
预占可能发生在发送前，不能由此声称准确物理POST数。旧Router指标包含共享账本中的Source
调用时标明共享Router总量，不当作旧业务Worker数。

## 当前验证证据

Root首次make lint exit0。typecheck初轮检查245文件，仅私有domain两处类型推断失败；
等价展开谓词后仅该失败文件mypy exit0，无字段/schema变化，未重复其余已通过目标。
更早组件与局部修复结果只作组件证据，尤其并行期间全局指纹变化的轮次不冒充整卡结果。

首次注册原始日志位于`evidence/SAV1-09/20261112T125824Z-1f26ecbe/command.log`；
result.json和junit.xml均已生成且未改写。命令、原始FAIL、唯一修复节点和静态版本见
[交付记录](SA09_ACCEPTANCE_20261112.md)与[机器摘要](accept/SAV1-09/accept.json)。
只读交付审计PASS：143引用、38迁移checksum、6个固定H02 blob、旧SQL/财务AST、
历史失败/用户文件均匹配；临时PG/会话/故障容器均0，主库新六表0行。

200例合成草案及proposed_holdout仍待操作者复核，不是独立语义holdout。真实平台、付费、
编码、外发、发布、完整系统安全与长期门保持NOT_RUN；本轮实现不代表系统已上线。
SA09等待独立验收，SA10未启动。
