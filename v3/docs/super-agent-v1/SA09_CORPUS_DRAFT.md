# SA09 合成路由标注草案：等待操作者逐例复核

本文件对应 [operator-review-draft-v1.json](../../fixtures/super-agent-v1/sa09-routing/operator-review-draft-v1.json)。状态为 `DRAFT_SYNTHETIC_OPERATOR_REVIEW_REQUIRED`：200 条消息、来源和权限事实均由开发者编写；不是平台采集记录，也不是已审批的 gold 标注。`operator_review=null`、`independent_holdout=false`，真实模型运行和语义评测均为 `NOT_RUN`。未产生模型预测、precision/recall、真实 Task/Run、付费调用、外发或发布。

## 数量与分母

| split | explicit | ambiguous | quoted | automation | 总数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| development | 25 | 25 | 25 | 25 | 100 |
| proposed_holdout | 25 | 25 | 25 | 25 | 100 |

每个 split 的拟定动作分布相同：

| 类别 | create | attach | ask | observe | ignore |
| --- | ---: | ---: | ---: | ---: | ---: |
| explicit | 15 | 10 | 0 | 0 | 0 |
| ambiguous | 0 | 0 | 10 | 15 | 0 |
| quoted | 0 | 0 | 0 | 5 | 20 |
| automation | 5 | 0 | 0 | 10 | 10 |
| 合计 | 20 | 10 | 10 | 30 | 30 |

每组 actionable 为 40（create/attach/ask）；not-new-task 为 80（所有非 create）；non-actionable 为 60（observe/ignore）。前两项是不同评测分母，有 attach/ask 的交集，不能相加后解释为样本总数。这里只检查草案分母，没有运行评分。

## 消息、受信假设与哈希

`corpus` 采用现有 `RoutingCorpus` / `RoutingCase` 格式；原有八条开发 fixture 保留。`case_contexts[case_id]` 将受信前提与消息正文分开，记录合成来源、作用域、定向状态、当前身份、Receive/model_input、仓库、规则、候选目标、时效和重复等事实。正文中的自称身份、转述授权或 bot 命令不构成这些事实。

这些权限均明确标为 `synthetic-assumption-awaiting-operator-review`，没有真实审批、签名或凭据。automation 的每组五个 create 另有当前匹配的合成管理员规则假设；它们不是依据 bot 正文取得权限。规则也必须由操作者核对，不得直接转成生产授权。

每组十个 attach 使用稳定 UUID，正文明确给出当前目标，合成上下文限定仓库、群、当前 controller 与 follow_up。共 20 个 UUID 仅是声明式测试引用，`persisted_business_object=false`；没有创建或冒充真实任务与运行。

每条 `source_case` 等于 `synthetic-` 加完整 context 的规范哈希；context 内含消息正文哈希。`corpus.case_payload_sha256` 再绑定完整 cases，因此同时绑定正文、标签和这些受信假设。上下文不含 gold 动作字段，也没有把标签当作模型输出。当前 case payload SHA256：

`19c304b5fca03be072ce620bbf1945887407f52ec4974c1b2a7ce92e8f7bee53`

## 拟议分组与复核边界

development 采用订单、会话、来源授权话题，模拟时间为 2026-04-01 至 2026-04-26 UTC；proposed_holdout 采用任务编排、文件同步、恢复话题，模拟时间为 2026-09-01 至 2026-09-26 UTC。topic、source_case、来源讨论 ID 和领域没有跨 split 共享。正文逐条编写；机械检查会去掉 UUID、仓库名和数字后排查复制文本，并限制跨组字符三元组相似度。

开发者已经看过两个 split。`proposed_holdout` 只是待审分组，不是独立冻结、未见过或已通过的 holdout。领域和字面差异不能证明语义独立：引用冒充、历史重复、身份失效和跨作用域等安全主题仍在两组出现，须由操作者检查潜在近似场景。不得据此调参后仍宣称独立留出成绩；如参与调参，应另建并独立冻结留出集。

操作者应逐例核对消息与受信假设是否一致，确认 Receive/model_input、动作、仓库、精确目标与各项权限标签。尤其需要裁定历史或重复消息的 ignore/observe 边界、缺仓库或含糊请求的 ask/暂存边界、失效身份的当前有效角色，以及管理员规则的触发范围。`actor_role` 表示草案的记录类别；当前有效身份和角色另见 context，不能仅凭该枚举授予权限。ask 是拟定路由意图，不授权实际发送澄清消息。

复核时应保留原始草案、异议及裁决记录，产出可追溯的操作者审阅引用和证据哈希，再独立确定实际评测使用范围。不得把机械测试通过解释为操作者已同意这些假设或标签。

## 本轮校验范围

新增 [test_super_agent_sa09_corpus.py](../../tests/test_super_agent_sa09_corpus.py) 只校验 schema、200 条完整性、各组类别和动作分布、分母、唯一性、context/正文/完整 cases 哈希、模拟分组、稳定目标声明及未知操作者审阅状态。它不调用模型、不生成预测、不运行 scorer，也不产生真实语义成绩。实际语义与平台验证仍须按 SA00 协议分别执行和记录。
