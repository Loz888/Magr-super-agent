# SA09 来源 Router 与受限 Eval

状态：IN_PROGRESS，本地工程交付完成，等待独立验收。SA08已由用户接受原3项独立复验和
新增6项边界检查；SA09共享范围已获Harness固定`0e0adc6`第32节实际确认。
本卡只交付来源候选、模型协议、确定性校验和影子决定，不激活真实来源、付费、发送或发布。

## 复用与边界

H02采用固定实现`169925f`，正式接收元数据`fee0875`。只选择性集成四个源文件、原受控
测试和必要的静态能力样本，首次H02/H01/H00兼容58 PASS；未导入H12、0037或活动树内容。
Source模型端口只复用公开SSEDecoder、ModelCapabilities和estimate_tokens，不构造Harness
ModelRequest、TaskBinding、Run或Workspace。字符估算不当作精确Token证明，完整请求另有
UTF-8字节与Root Router许可的保守上界。

继续使用原Engine mutex、advisory lock、持锁PG连接和事务，以及原router_batches/router_calls、
RouterBudget和SA07共享预算。原三处财务结算AST、旧DTO、ToolAuthority和历史迁移保持。
0039仅增加六个Source Eval事实表；本副本共有38份迁移，即0001–0036、0038及0039。

Source批次带`source-router-snapshot-v1`标记，旧兼容外壳只有空threads数组。旧Routing的
prior/failed/cache查询排除该标记，wrap/validate/commit拒绝它，save/failure不能改写。
原RouterWorker只增加受信pipeline和decoder成对注入，缺省保留旧流程。Source解码器读取
PG JSON时使用SourceDecision.model_validate_json，不能把存储UUID字符串当严格Python对象。

## 候选与当前权限

只接受现有FastPath批次ID，控制优先级100不送模型。每个批次最多16条canonical输入，
目标最多3个。准备、预占和提交时重新构造当前快照，核对Source、generation、FastPath/
Eval配置、policy、identity、input版本、batch成员revision、TTL和当前密文。追加成员、
编辑、删除、撤权、预算修订或目标epoch/revision改变都会使旧快照失效。

仓库只能来自可信平台hint与签署Execution边界，或唯一明确签署的仓库边界；不从正文猜测。
没有明确仓库、上下文或预算时保持pending并给固定原因，不补拉正文、不跟随URL。
精确目标还要求所有成员当前共同的task_controller、准确Ticket membership及follow_up
Admission；角色本身或explicit_request许可都不能替代跟进授权。目标引用绑定原Thread/
Run/epoch/Mandate hash，但不授予该Run任何执行权。

提案版本为source-router-proposals-v1，只能给group_id、ignore/observe/attach/create/ask、
准确target_id及配对枚举reason_code。额外正文、rationale、actor、policy、epoch、预算、工具
和发布字段均拒绝。程序构造source-router-decision-v1，绑定输入和完整snapshot hash；
所有决定均为would_*且eligible_for_execution=false。模型失败不会创建任务。

## 模型、预算和恢复

SourceEvalModel需要显式注入client、credential和原RouterPermit，默认无provider、无网络
客户端或环境激活入口。profile_ref准确绑定许可profile hash，模型/价格/提示版本逐项核对。
签署Source配置的上下文/输出字节上限也限制runtime对象。POST前要求原连接IDLE，拒绝在
调用者外层事务/savepoint内执行，预占必须durable后才发送；HTTP不在业务事务内。

只发一次固定HTTPS请求，不自动重试或跟随重定向，没有tools。流有总字节、帧、输出和
时间上限，要求权威finish、完整严格JSON及合法usage。已知费用和提案合法性分开：
known-invalid仍结算真实观察费用，缺回执/usage保留预占，不能当已知零费用。

原RouterBudget.settle只收cost/usage，result保持None；安全提案与结算在同一个原Engine
外层事务原子提交。原始响应不调用RouterBudget.response，不进普通CAS或新事实表。
来源正文只在当前鉴权的临时ModelContext内组装，错误只给固定码，永久记录限于ID、hash、
revision、授权快照和严格枚举。unknown/reserved及known-invalid按原canonical输入阻止
借新配置、成员revision、批次或Run重发。原Engine.recover处理reserved→unknown、已知
model_running→prepared；已知安全结果可恢复复用，ready决定不再次调用provider。

Source政策没有独立计费窗口，因此其额外total/router上限保守计入本项目原账本全部历史
已知费用和reserved/unknown，再叠加当前SA07项目/组织时窗约束，不退款、不改旧算法。

## 管理与计数

默认disabled。配置保存需要当前主人cookie、Origin/CSRF与CAS；服务事务中再次检查会话。
所有接口no-store，不接受Source正文、模型提案、身份/权限断言、预算消费或运行模式字段。

| 方法 | `/api/v3/source-eval` 下的路径 | 行为 |
| --- | --- | --- |
| PUT/GET | /{source_id} | 签署保存/读取设置及revision |
| POST | /{source_id}/evaluate | 仅已有fast_batch_ids；202表示排队，语义仍NOT_RUN |
| GET | /{source_id}/preview | 最多100项当前/过时快照预览 |
| GET | /{source_id}/decisions | 最多100项无正文影子决定 |
| GET | /{source_id}/metrics | Source调用账本与预占/已知费用 |

HTTP排队缺省fixture，不能用请求正文选择付费模式；live队列只能由受信runtime显式提供
模式和已有许可。本卡未启动后台worker或真实模型。FastPath.consumed_at仅表示确定性观察，
不表示Source语义评估或Worker已应用。

metrics区分recorded_model_attempts、known_billed_calls、unconfirmed_model_attempts；
发送前崩溃也可能留下预占，因此不把账本行数称为准确物理POST数。旧Router总量自然包含
共用账本的Source调用，表示共享Router总量，不是旧业务Worker数量。

## 评测与交付证据

评分器固定完整case/标签hash，显式、模糊、引用和自动化分别报告。重复/未知case结果
拒收；未运行、失败、unknown保留在原分母。零预测create时precision为undefined，
不记100%；不可判关联单独报告，不能随机选目标记成功。报告分子/分母、Wilson小样本
区间、越权模型输入、Shadow副作用、已知费用和未知预占，不能只提交最好的一轮。

合成草案和受控协议检查不等于真实语义质量。SA00的dev100/独立holdout100仍要求操作者
逐例复核和冻结；当前开发者可见的proposed_holdout不称独立holdout。模型协议和工程成绩
分别记录，真实平台、付费、外发、发布、完整系统安全与长期门保持NOT_RUN。

确认依据、原始失败、组件范围与后续首次整卡结果位于
`evidence/super-agent-v1/SAV1-09/`。首轮冻结1777项为1776 PASS、1 FAIL；唯一旧SA06 future
时间负例仅隔离测试时钟后定向1 PASS，生产源码与全部原断言保持。未重复通过项，不称修复后全量PASS。
当前源码93e9a9d，详细版本、原失败、静态检查和清理见[交付记录](SA09_ACCEPTANCE_20261112.md)及
[机器摘要](accept/SAV1-09/accept.json)。SA09未独立通过前不启动SA10。
