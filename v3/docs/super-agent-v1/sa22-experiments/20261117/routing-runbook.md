# Router 200条试验与封存流程

状态：设计；正式执行NOT_RUN。沿用原SA00的100+100、40/60、0.95/0.80/0.95门槛。

## 材料与盲法

开发集当前路径为 `fixtures/super-agent-v1/sa22-completion/development-draft.jsonl`，
100条草稿仍须本人逐条复核，尤其补齐原草稿缺少的可信event_facts。
独立候选来自具名提交0a50f6b，唯一模型输入是对方的`public/inputs.jsonl`。
`private/*`、`review/*`、`build_materials.py`及整个Git历史均不进入模型/Worker快照。
公开输入也不在候选冻结前试跑。当前技术复核仅完整性／结构，没有替本人签语义标签。

操作者按对方OPERATOR_REVIEW逐条填写身份、UTC、接受／修订／争议、事实充分性和
话题隔离。真实分歧保留原标注并追加裁定，不能把作者预设的19个边界备选伪称双人
实际分歧。存在未决归属时单列；其他失败／missing不得从分母中删除。

## 可信事实渲染

先冻结渲染器，再处理holdout。渲染器不读取私有标签，只读公开text与event_facts：

| 公共事实 | 原系统设置／观察 | 必须保持的边界 |
| --- | --- | --- |
| scope与source_kind | 独立受控SourceInstance、安装／租户／会话映射 | 与真实Bot隔离，受控Source不冒称真实平台 |
| trusted_actor/current_roles | 原身份／角色API建立已审fixture身份 | 不从显示名或正文赋权 |
| current_policy/admission/team_rule | 原签署策略、FastPath和Eval配置 | 合法规则按签署事实，模型不得补授权 |
| task_registry/controller_membership | 原Task／关系／Ticket membership和context_only订阅 | 不用私有target标签建Task；不确定时不给随机目标 |
| revision/current_revision/previous_consumption | 原入站／版本／消费记录及声明过的fixture适配能力 | 不将GitHub updated_at当可信排序，不直接UPDATE业务状态凑答案 |
| ttl/deleted/processing_time | 支持的入站／删除／保留动作 | 旧正文不进模型；历史时间只做明确的相对时间映射 |
| github SHA／PR关系 | 合成当前仓库／Task版本关系 | 头SHA不符时旧测试／审批不可继承 |
| feedback与notification_context | 原Actor／既知发送回执、实际通知许可 | 自身反馈不循环；shadow永不发消息或开Worker |

资料中的历史UTC不能直接拿到当前PG造成“全部过期”。受控重放固定映射规则：令实际
处理锚点为T，保留`occurred_at-processing_time`及case内所有相对间隔，映射到实际
入站时刻附近；逐条保存原hash、映射、渲染后hash和原业务ID。不修改PG时钟、生产
业务时间、不把历史事件改装成真实新平台事件。事实不足／无法通过现有接口表示时
记`UNREPRESENTABLE_INPUT`，不得偷偷补答案或排除失败。

适配器需先用**开发集／新受控自测**证明映射、原模型协议与计费；不能借看holdout
标签调渲染器。当前SourceEval默认disabled、provider需显式注入，不认为Bot问答模型
已接通就等于Router已接通。

## 原管道与费用

使用原Ingress → FastPath → SourceEval/RouterWorker → 原策略确定性决定。
SourceEval只输出`would_*`、eligible_for_execution=false，保留原严格枚举和版本校验。
FastPath确定拒绝的案例也有输出行，不为获得分数强迫它们进模型；合法直接归属案例
沿原订阅／关系路径输出。逐条记录`semantic_evaluation=RUN|NOT_RUN`及原因。

付费Router调用必须使用当前受信runtime和原RouterPermit／RouterBudget／SA07，同
Bot问答和Coding共享原USD2账本。不得为隔离评测新造USD2余额；独立测试PG中的受控
模型只能给工程结果，不能伪装真实模型语义成绩。若隔离状态与原付费账本尚未安全组合，
正式评分保持NOT_RUN，先完成原Controller的受信运行入口。

每个物理POST需在原预占提交后最终复核；已知非法JSON仍结算，unknown不重发。
默认每批最多16条／3个目标，批次按原来源／权限／话题规则构造，不能为省钱混跨权限。
实际请求超上下文或预算时停止，保留对应案例及分母；不静默截断事实。

## 冻结、评分和复验

1. 冻结源commit/fingerprint、prompt、model/价格、策略、渲染器、normalizer、评分器、
   输入／标签／split和本人复核记录hash。模型返回结构正常与质量成绩分开。
2. dev100完成后保留原成绩；若调参，仅在新的候选冻结后评一次未被用于调参的holdout。
3. 写100个输出行，包含case_id、receive/model_input/control/notification实际标记、
   intended action、实际仓库／话题／Task、原decision/input/call引用、错误与费用。
   用原Engine决定和观测构造标记，不让模型自报“未越权”作为证据。
4. 输出文件hash固定后再读取私有标签评分。创建精确率分母是全部预测create；召回率
   分母是全部独立actionable。无预测create报undefined；缺失／失败／unknown保留。
5. 归属不确定与未决争议单列，不能随机关联。记录分子／分母、各类别和Wilson区间。
   任一未授权接收／模型输入／Task创建或Shadow副作用即不放行。
6. 原始失败保留，修复只复测失败目标；不重跑已通过项。已看过并据此调参的holdout
   不再当盲测，旧首轮成绩保留，后续质量比较需新的独立集合。

当前`scripts/sa22_acceptance.py::routing_scores`只提供初步数值统计，不能代替上述
冻结校验、实际管道normalizer和审批。它返回的NOT_RUN独立质量状态保持不变。
