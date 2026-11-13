# SA13 本地工程交付，等待独立验收

状态：**本地工程检查已闭环，READY_FOR_INDEPENDENT_REVIEW**；SA13 的独立验收尚未运行，整卡不标 DONE，SA14 不启动。执行修复候选为 `47fb162`，最后两项复验绑定源码 SHA256 `2b32953d1210e97e0bb8584c41f612a681edaed261dfee0e61be17f37977309a`，前后一致。最终交付源码候选为 `f53f4233721b095844c8376695f774195754ab40`，指纹 `e7f8dd7b37357dff2163834a034c3b6722565d097ea052a0b7b65d5d940ef956`。其 src/tests/migrations/scripts/schemas 与最后功能复验47fb162逐字节一致，差异仅为就绪和产物元数据；原始分版本成绩不改写。

首次实际正式检查共 185 项：156 PASS、29 FAIL。之后只复验原失败项，并对新覆盖的 27 项边界执行一次首次检查；185 个原目标和 27 个新目标共 212 个独特检查均已有相应版本的通过证据，原失败完整保留。没有重跑已通过项，也不宣称最新候选单轮 212 PASS。

## 实施范围与权威

Source Coding 已组合到原 Engine、同一持有 advisory lock 的 PG 连接及 mutex。新 `source-coding-mandate-v1` 冻结原 Thread／Ticket／Run／Mandate、canonical Source 引用、身份／角色／策略版本、仓库与物化代码树、路径、命令、环境、预算、上下文、验收集和期限。旧调查 Run、只读 grant 和已有执行事实不会被升级。HTTP 只有六个 metadata 接口：四个 Owner 接口沿用当前 cookie、Origin／CSRF 和事务内会话复查；两个 Worker 接口要求独立、可撤销且绑定当前 Assignment／Session 的 bearer，不能用 Owner cookie 替代。

原业务对象与费用仍由 Engine 裁决。私有适配器调用原 `usage_calls`、SA07 组织／项目预占及原 `Engine.settle_call`，没有复制一套 Task、账本或资金池。unknown 继续占用原预占且不自动重发；已经返回的实际用量即使遇到撤权也须如实结算。来源正文不写入旧 question、触发 payload 或旧输入快照；需要恢复的模型响应和 checkpoint 有界加密，解密前复查当前权威，失效后不可读并可擦除为最小无正文事实。

默认 runtime、provider、cipher、Worker、来源监听和监督线程均未激活。受控模型通过明确的 `ControlledCodingModel` 端口注入，普通网络模型适配器不能被当作 fixture provider。没有通过环境变量或 fixture 预算开启付费、外发或发布的入口。

## 共享确认与正式集成

Harness 确认提交为 `617e14a52d65c5276bcd3ef87dbd0f05b4a3c08d`，交接文档第 38 节。该确认覆盖 0046、六个接口和本 SA 副本的 Engine／API／Scheduler／contracts 最小接线；没有导入对方活动工作树。正式 `0046_source_coding.sql` 与批准草案逐字节一致，SHA256 为 `379e9de4a018c7f38cc3615eae31b9c9d9e44ebdf16cbcf3c101c81d9874f212`，新增六类 Coding 事实。原 42 份 SQL 与 SA12 候选 `6b48b7d` 逐字节一致；主库仅安装新增 0046，43 份迁移 checksum 全部一致，六张新表均为 0 行。0045 仍归 Harness。

固定 H11 提交 `d3f3457b3d9dfab68bc73a40add299e4e633fbeb` 的 `coding_loop.py`、`long_context.py` 和新命名空间 `source_coding_adapters.py` 保持固定字节；`incremental.py` 仅应用已确认的 full-content 兼容补丁。旧两参数 reference registry 保留；Source 使用必须提供 authority 的独立严格 registry。`WorkspaceEdit.content` 仍必填，任何 supplied `replacements` key 均拒绝，包含一般 Mapping、null 和空列表；原 R5 worktree 不改。旧 ExecutionReceipt 仍可读取，新增 provenance 默认为 null，新的完整序列化及其 hash 不伪称与旧存档完全相同。

四个共享文件在批准补丁之后仅有两项无行为 followup：API 将新增 import 排入词法顺序；Scheduler 用 `typing.cast` 标注既有返回表达式。逆转这两项后，四文件均恢复批准的 SHA256。批准补丁与两个差异均保留，不能以新格式覆盖原协调记录。

## 本片受控 Coding 边界

实际接线使用 H03 独立工作区和 SA12 bound H06 Broker 的持久资源 operation。命令的完整身份、owner labels、当前 lease／epoch 和实际代码树在最终派发处校验；没有调用宿主 LocalExecutionBroker 或直接随机构造 Broker 身份。可信 verification 需要同一原 Run／Assignment／Session／epoch／operation、实际 H03 tree、environment、profile、验收集和可信执行 journal；裸 Worker JSON、报告或自报测试成功都不构成该证明。即使形成 Coding verification，也不会调用旧 GitHub 发布路径或自动将原业务 Task 标为成功。

首片刻意限制为现有公开 `fixture_git` 中既有纯函数 `.py` 文件的 bugfix。可写文件须通过纯函数 AST 门，禁止 import、Attribute、打印假测试计数、SystemExit、decorator、raise、global 等不在此小切片内的构造；这不是通用 Python 安全性或通用修复能力声明。实际命令必须等于受信 baseline argv，cwd 只接受固定内核基线形式或受信工作子目录，再归一化为该目录；baseline、working directory、验收要求与 AST gate 版本共同绑定 acceptance SHA256。测试文件与验收器不能由模型修改来证明自身正确。

Source／身份／角色／策略／上下文／预算配置、原目标、Session、Assignment、租约和 epoch 均在当前 PG 时间下复查。预占提交后发生的策略撤销或 context_provider 撤权，必须在实际模型调用之前阻止旧正文；实际响应之后撤权则保留真实费用，拒绝旧工具与结果提升。源码写入前先持久化加密 preimage／postimage 意图，再执行逐文件 atomic replace，恢复只承认精确的原像或后像，不覆盖外来内容。

显式 `SourceCoding.maintain` 仅使用原 Engine 连接和有界游标，不新增公开接口或默认线程。server retention 为严格整数 1–1800 秒，默认 1800 秒，且不越当前 Source／Mandate 有效期。payload 读取先在独立短事务中提交过期／撤权擦除，再进入可能拒绝访问的事务；拒绝不能回滚已经完成的擦除。初次执行还复查真实 H03 manifest、registration 和获审初始树，合法存储所有者的额外编辑也不能取代获审代码树。

完整 Engine 恢复会 fence 旧 epoch、停止旧 attempt、保留 unknown 与真实财务事实，并由原 SA12 资源事实约束清理。它不会自动继续旧 Worker、重发模型请求或把 partial write 当成成功。相同控制器下的部分写恢复也必须重新取得当前权威，只对账原 preimage／postimage，不以恢复名义扩大权限。

私有仓库、真实 GitHub snapshot／App token 通道、通用新建文件、feature／refactor、run_app、真实模型 Coding、发布与长期运行均不在本片支持范围。

## 分版本检查与修复

| 阶段 | 绑定源码／记录 | 当前结果 |
| --- | --- | --- |
| 原私有契约准备 | 独立的准备证据 | 139 PASS，未重跑，不替代正式集成 |
| 原 H03 只读 façade 准备 | 独立的准备证据 | 13 PASS，未重跑，不替代正式 Runtime／恢复 |
| 正式 runner 首次调用尝试 | `683fabfa…`；node ID 被误按完整文件路径校验 | 未进入 pytest，保持 NOT_RUN，不计入 185 项成绩 |
| 首次实际正式 SAV1-13 | `aa112e5a…`；运行前后指纹一致 | 185 项：156 PASS、29 FAIL |
| 仅原 29 项失败复验 | `8fed2e3c…`；`20261113T113913Z-failed29-recheck-f869a8` | 29项：26 PASS、3 FAIL |
| 仅原 3 项失败复验 | `8e446c9`／`1099db71…` | 3 项：1 PASS、2 FAIL |
| 新生命周期与初始树边界首次检查 | 同上，与原 3 项组合执行 | 新 27 项一次 27 PASS；组合共 30 项：28 PASS、2 FAIL |
| 仅剩余 2 项失败复验 | `47fb162`／`2b32953d…`；前后指纹一致 | 2 PASS、0 FAIL，exit 0 |
| 独立验收 | 待独立执行者核验最终交付候选 | NOT_RUN |

原 29 项失败分为 11 项错误码断言和 18 项受控 fixture lease 缺少 execution_epoch。前者错误地将异常显示文本当作 `DomainError.code`，修复改为检查结构化 code，不放宽生产拒绝语义；后者补足原 lease 所需 epoch，没有降低生产 guard。随后剩余的 3 项暴露真实 H03 接线缺陷：多文件写入完成后未推进原 manifest 当前 tree；部分写在进入 journal 恢复前被普通重开检查挡住。私有修复只在原 pending journal、当前授权和精确加密 preimage／postimage 存在时允许恢复入口，完成后安全推进 manifest 并保留原 baseline。最后 2 项修复为向恢复入口补入实际观察到的 expected_tree，继续拒绝外来内容。每一轮只重跑原失败项，原 156 项与新 27 项通过均未重复。

lint／typecheck 的原始失败记录保留，修复后仅检查原失败文件或目标。生命周期新增 Core SQL 行长产生的唯一 E501 只拆分相邻字符串，AST 等价；该唯一失败 lint 目标随后通过。最后两项恢复修复之后没有重跑已通过的 mypy 或其他通过项。

三个真实 SIGKILL 目标分别为：持久化多文件 images 后、首次物理写入后但回执前、已知模型结算后但 Worker 收取回执前。这三个目标在原 29 项复验中均 PASS，证据含实际子进程退出、持久 marker、恢复后旧 epoch 拒绝、无请求重发／退款／假完成及资源清理；没有用手动改写数据库状态替代进程边界。

最后盘点确认主库 43 份迁移 checksum 一致、六张 Coding 表均 0 行；默认 runtime／cipher 为 None、profiles 为空，Scheduler driver 为 None。临时 PG、会话、本卡容器及 crash actor 均无残留，受保护用户文件 SHA256 仍为 `f4adf9605c87d6256bd2ec17b8436da8a7bced763a8e03b185a8cfe19d34a29f`。原主库环境中的 `allow_paid`／`allow_external_write` 均为 true，保持原配置；这些开关不构成 Source Coding 授权，本轮没有真实付费或外发。

首次默认惰性元探针误将上述既有开关预期为 false，失败记录完整保留为 `main-inert-initial-failed-expectation.json`。纠正预期后核验的是 Coding 默认无 provider／runtime／profile、无执行事实及无残留，没有修改用户配置或放宽执行门。

修复候选的只读 metadata 审计核对了 13 份清单、311 个 artifact 引用，均无缺失或 hash 不符；原 42 份 SQL、固定内核、共享补丁差异、旧 accept 历史、SA12 已接受的 14 份独立原始证据及受保护用户文件均匹配。该审计只证明指定时点的字节绑定，不构成独立验收或 MVP 成绩；最终 metadata 收尾后的清单由最终交付记录另行绑定。

## 能力门与证据

本轮受控范围的**本地工程检查已闭环，等待独立验收**；完整系统 policy/security、语义、真实来源、付费、真实 Coding、外发、发布及长期运行保持 **NOT_RUN**。固定 H12 `88c75b8dac75da7e7d0c283a451c0ff5a3a3c9b0` 的 live_agent／MVP 仍为 **FAIL**；模型连通、fixture Runtime 或环境恢复不改变此状态。SA13 独立验收未运行，SA14 不启动。

[共享范围](sa13-proposal/SA13_SCOPE.md) · [首次实际正式结果](../../evidence/SAV1-13/20261113T112859Z-455078f1/result.json) · [首次 JUnit](../../evidence/SAV1-13/20261113T112859Z-455078f1/junit.xml) · [原 29 项复验](../../evidence/super-agent-v1/SAV1-13/start-20261113T092527Z-3fe7966b/20261113T113913Z-failed29-recheck-f869a8/commands.json) · [原 3 项及新 27 项组合轮](../../evidence/super-agent-v1/SAV1-13/start-20261113T092527Z-3fe7966b/20261113T120323Z-failed3-and-new-boundaries-initial-397da3/commands.json) · [剩余 2 项复验](../../evidence/super-agent-v1/SAV1-13/start-20261113T092527Z-3fe7966b/20261113T120833Z-failed2-recheck-dc7cc0/commands.json)

[主库仅安装 0046](../../evidence/super-agent-v1/SAV1-13/start-20261113T092527Z-3fe7966b/main0046-install.json) · [默认惰性与清理盘点](../../evidence/super-agent-v1/SAV1-13/start-20261113T092527Z-3fe7966b/main-inert-and-cleanup.json) · [元探针原错误预期](../../evidence/super-agent-v1/SAV1-13/start-20261113T092527Z-3fe7966b/main-inert-initial-failed-expectation.json) · [修复候选只读产物审计](../../evidence/super-agent-v1/SAV1-13/api-implementation-20261113/final-readonly-audit-20261113T121131796892Z.json) · [历史配置与批准后差异](accept/SAV1-13/history/)

最终收尾核对13份清单、313个产物引用，0缺失／0SHA不符；源码归档及校验见[最终完整性记录](../../evidence/super-agent-v1/SAV1-13/start-20261113T092527Z-3fe7966b/final-integrity.json)。机器摘要和逐例账本分别见[accept.json](accept/SAV1-13/accept.json)、[case-ledger.json](accept/SAV1-13/case-ledger.json)。原185项与新增27项的每个目标均只有其首次PASS之前的FAIL复验，PASS之后从未重复。
