# SA13 本地工程交付草稿：原 3 项失败复验准备中

状态：**IN_PROGRESS，尚未放行独立验收**。本稿不宣布正式工程通过。最终候选提交、修复后的源码指纹、分版本成绩与清理盘点由本轮执行者在复验结束后补齐；SA14 不启动。

首次实际正式检查绑定源码 SHA256 `aa112e5a6338517b7154bb8df59fe04e624f089e35725d44987d85015058a0b0`，运行前后指纹一致。该次共 185 项，156 PASS、29 FAIL；原始失败、JUnit 和命令日志完整保留。原29项定向复验已结束：26 PASS、3 FAIL，指纹8fed2e3c…前后一致；原3项修复后的复验尚未执行，不将历史通过项与后续修复成绩合称为一次全量通过。

## 实施范围与权威

Source Coding 已组合到原 Engine、同一持有 advisory lock 的 PG 连接及 mutex。新 `source-coding-mandate-v1` 冻结原 Thread／Ticket／Run／Mandate、canonical Source 引用、身份／角色／策略版本、仓库与物化代码树、路径、命令、环境、预算、上下文、验收集和期限。旧调查 Run、只读 grant 和已有执行事实不会被升级。HTTP 只有六个 metadata 接口：四个 Owner 接口沿用当前 cookie、Origin／CSRF 和事务内会话复查；两个 Worker 接口要求独立、可撤销且绑定当前 Assignment／Session 的 bearer，不能用 Owner cookie 替代。

原业务对象与费用仍由 Engine 裁决。私有适配器调用原 `usage_calls`、SA07 组织／项目预占及原 `Engine.settle_call`，没有复制一套 Task、账本或资金池。unknown 继续占用原预占且不自动重发；已经返回的实际用量即使遇到撤权也须如实结算。来源正文不写入旧 question、触发 payload 或旧输入快照；需要恢复的模型响应和 checkpoint 有界加密，解密前复查当前权威，失效后不可读并可擦除为最小无正文事实。

默认 runtime、provider、cipher、Worker、来源监听和监督线程均未激活。受控模型通过明确的 `ControlledCodingModel` 端口注入，普通网络模型适配器不能被当作 fixture provider。没有通过环境变量或 fixture 预算开启付费、外发或发布的入口。

## 共享确认与正式集成

Harness 确认提交为 `617e14a52d65c5276bcd3ef87dbd0f05b4a3c08d`，交接文档第 38 节。该确认覆盖 0046、六个接口和本 SA 副本的 Engine／API／Scheduler／contracts 最小接线；没有导入对方活动工作树。正式 `0046_source_coding.sql` 与批准草案逐字节一致，SHA256 为 `379e9de4a018c7f38cc3615eae31b9c9d9e44ebdf16cbcf3c101c81d9874f212`，新增六类 Coding 事实。原 42 份 SQL 与 SA12 候选 `6b48b7d` 逐字节一致，当前源目录共 43 份，0045 仍归 Harness。

固定 H11 提交 `d3f3457b3d9dfab68bc73a40add299e4e633fbeb` 的 `coding_loop.py`、`long_context.py` 和新命名空间 `source_coding_adapters.py` 保持固定字节；`incremental.py` 仅应用已确认的 full-content 兼容补丁。旧两参数 reference registry 保留；Source 使用必须提供 authority 的独立严格 registry。`WorkspaceEdit.content` 仍必填，任何 supplied `replacements` key 均拒绝，包含一般 Mapping、null 和空列表；原 R5 worktree 不改。旧 ExecutionReceipt 仍可读取，新增 provenance 默认为 null，新的完整序列化及其 hash 不伪称与旧存档完全相同。

四个共享文件在批准补丁之后仅有两项无行为 followup：API 将新增 import 排入词法顺序；Scheduler 用 `typing.cast` 标注既有返回表达式。逆转这两项后，四文件均恢复批准的 SHA256。批准补丁与两个差异均保留，不能以新格式覆盖原协调记录。

## 本片受控 Coding 边界

实际接线使用 H03 独立工作区和 SA12 bound H06 Broker 的持久资源 operation。命令的完整身份、owner labels、当前 lease／epoch 和实际代码树在最终派发处校验；没有调用宿主 LocalExecutionBroker 或直接随机构造 Broker 身份。可信 verification 需要同一原 Run／Assignment／Session／epoch／operation、实际 H03 tree、environment、profile、验收集和可信执行 journal；裸 Worker JSON、报告或自报测试成功都不构成该证明。即使形成 Coding verification，也不会调用旧 GitHub 发布路径或自动将原业务 Task 标为成功。

首片刻意限制为现有公开 `fixture_git` 的普通文件 bugfix。可写 Python 文件须通过纯函数 AST 门，禁止 import、Attribute、打印假测试计数、SystemExit、decorator、raise、global 等不在此小切片内的构造；这不是通用 Python 安全性或通用修复能力声明。实际命令必须等于受信 baseline argv，cwd 只接受固定内核基线形式或受信工作子目录，再归一化为该目录；baseline、working directory、验收要求与 AST gate 版本共同绑定 acceptance SHA256。测试文件与验收器不能由模型修改来证明自身正确。

Source／身份／角色／策略／上下文／预算配置、原目标、Session、Assignment、租约和 epoch 均在当前 PG 时间下复查。预占提交后发生的策略撤销或 context_provider 撤权，必须在实际模型调用之前阻止旧正文；实际响应之后撤权则保留真实费用，拒绝旧工具与结果提升。源码写入前先持久化加密 preimage／postimage 意图，再执行逐文件 atomic replace，恢复只承认精确的原像或后像，不覆盖外来内容。

完整 Engine 恢复会 fence 旧 epoch、停止旧 attempt、保留 unknown 与真实财务事实，并由原 SA12 资源事实约束清理。它不会自动继续旧 Worker、重发模型请求或把 partial write 当成成功。相同控制器下的部分写恢复也必须重新取得当前权威，只对账原 preimage／postimage，不以恢复名义扩大权限。

私有仓库、真实 GitHub snapshot／App token 通道、通用新建文件、feature／refactor、run_app、真实模型 Coding、发布与长期运行均不在本片支持范围。

## 分版本检查与当前未决项

| 阶段 | 绑定源码／记录 | 当前结果 |
| --- | --- | --- |
| 原私有契约准备 | 独立的准备证据 | 139 PASS，未重跑，不替代正式集成 |
| 原 H03 只读 façade 准备 | 独立的准备证据 | 13 PASS，未重跑，不替代正式 Runtime／恢复 |
| 正式 runner 首次调用尝试 | `683fabfa…`；node ID 被误按完整文件路径校验 | 未进入 pytest，保持 NOT_RUN，不计入 185 项成绩 |
| 首次实际正式 SAV1-13 | `aa112e5a…`；运行前后指纹一致 | 185 项：156 PASS、29 FAIL |
| 仅原 29 项失败复验 | `8fed2e3c…`；`20261113T113913Z-failed29-recheck-f869a8` | 29项：26 PASS、3 FAIL |
| 最终修复候选与独立验收 | 待本轮执行者填写 | NOT_RUN／未放行 |

原 29 项失败分为 11 项错误码断言和 18 项受控 fixture lease 缺少 execution_epoch。前者错误地将异常显示文本当作 `DomainError.code`，修复改为检查结构化 code，不放宽生产拒绝语义；后者补足原 lease 所需 epoch，不能通过降低生产 guard 来使 fixture 通过。原 156 项通过不重跑，原失败日志不覆盖。静态检查的首次失败和原失败目标定向复验另有原始记录；本草稿不将修复前后结果拼接成最新源码单轮全通过。

三个真实 SIGKILL 目标分别为：持久化多文件 images 后、首次物理写入后但回执前、已知模型结算后但 Worker 收取回执前。这三个目标在原29项复验中均PASS；必须由实际子进程退出、持久 marker、恢复后的旧 epoch 拒绝、无请求重发／退款／假完成及资源清理记录共同证明，不能只检查手动改写的数据库状态。

最终主库迁移安装与 checksum、临时 PG／会话／本卡容器和子进程残留、最终候选与原 156 项通过部分的源码等价范围，均由本轮执行者完成盘点后填写。此前只读 metadata 审计核对了原 42 份 SQL、固定内核、共享补丁差异、308 个 artifact 引用、旧 accept 历史及受保护用户文件；该审计不代替本次 Runtime、恢复或独立成绩，修复后的清单须重新机械绑定。

## 能力门与证据

本轮工程门仍为 **IN_PROGRESS**；完整系统 policy/security、语义、真实来源、付费、真实 Coding、外发、发布及长期运行保持 **NOT_RUN**。固定 H12 `88c75b8dac75da7e7d0c283a451c0ff5a3a3c9b0` 的 live_agent／MVP 仍为 **FAIL**；模型连通、fixture Runtime 或环境恢复不改变此状态。SA13 独立验收未运行，SA14 不启动。

[共享范围](sa13-proposal/SA13_SCOPE.md) · [首次实际正式结果](../../evidence/SAV1-13/20261113T112859Z-455078f1/result.json) · [首次 JUnit](../../evidence/SAV1-13/20261113T112859Z-455078f1/junit.xml) · [原 29 项失败复验命令](../../evidence/super-agent-v1/SAV1-13/start-20261113T092527Z-3fe7966b/20261113T113913Z-failed29-recheck-f869a8/commands.json) · [只读 metadata 审计](../../evidence/super-agent-v1/SAV1-13/api-implementation-20261113/delivery-readonly-audit.json) · [历史配置与批准后差异](accept/SAV1-13/history/)

第二轮剩余3项为真实H03接线缺陷：多文件写入完成后未推进原manifest的当前tree；部分写
在进入journal恢复前被H03普通重开检查挡住。私有修复只在原pending journal、当前授权和
精确加密pre/postimages存在时允许恢复入口，完成后安全推进manifest并保留原baseline。
另补尚未首次覆盖的初始树漂移、当前权限清理与真实TTL边界。维护端口只用原Engine连接，
在拒绝读取可能回滚的事务之外持久擦除，不退款或自动发起进程／模型。
