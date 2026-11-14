# SA14：持久输入、原资源 Job 与安全点

状态：IN_PROGRESS，私有契约和共享提案准备。本文不是 SA14 放行记录。
基线为 SA13 用户接受的 f53f423／c49cf1f，关闭文档 41f62c1。
依赖核对见本卡 evidence 的 fixed-h09-audit；仅使用已进入本副本的 SafePoint。

## 1. 固定复用与能力范围

H09 实现固定为 7d8f0ab5df1d4b98ff3140af553c11bc1ca74a1c，工程复核
096300eed8946086f99e331ae4c8c9c733108ff9。只读 617e14a 中的已提交验收记录。
H09 与已集成 H11 d3f3457 的 SafePoint AST 一致；复用 before_model／after_model。
不导入 H09 的文件 inbox.json、Store、Service、Ledger 或 SSE 服务；它们不能成为
Source 身份、正文保留期和 Engine PG 的第二权威。旧 H09/H10 产品发现不在本卡闭环。

仍仅允许显式服务端注入的公开 fixture_git、原 SA13 已有纯函数 Python 修复范围。
原真实 H03 工作区、SA12 bound H06 容器、H11 回路和原 Verifier 均保留。
H12 live_agent／MVP 仍 FAIL。真实来源、付费、模型外发、通知、发布及长期运行
均 NOT_RUN。本卡不提供实际数据库写入、网络扩权、私有仓库或真实外部写入口。

## 2. 同一原目标与收件箱

唯一业务写者为原 Engine 的同一持锁 PG 连接；所有 SQL 在短事务内。
原 Thread／Ticket／Run／Mandate／Assignment／Session／Workspace／epoch 不变。
AsyncScope 冻结这些 ID、Coding revision、Mandate／profile／初始 context hash。
SourceAsync 是原运行的输入和资源观察附属事实，不创建第二 Task、Run 或钱包。

Owner 用当前 cookie／Origin-CSRF 和 review_ref 显式把已获准 Coding Run 交给
异步模式。只接受原 active Assignment，后台执行仅由当前 Worker 显式 start。
默认无运行器、线程、监听、定时轮询或模型调用。旧 source-coding /worker/run
必须拒绝已被异步模式接管的运行，防止绕过新输入检查。

输入类型为 context／restriction／invalidation／job_progress，状态严格区分
received／applied／blocked。每条都有原范围 hash、序号、request hash、输入 revision。
来源输入只存 canonical input ID、来源／身份／策略版本和状态 hash；不复制正文。
Owner 只提交具体 deny 集，不能提交正文、argv、provider、token、权限或成功结论。
Job 完成和失效事实必须来自原资源回执或当前原权限变化，模型不能伪造。

普通输入最多 256 条／运行；安全失效事实不受普通配额拦截。工作视图最多 64 个
Source 引用，512 个安全点，128 个原 Job。达到上限明确 blocked／fenced，不能
静默丢弃限制后继续执行。分页与事件游标有界，只返回无正文元数据。

## 3. 归属与控制权限

不将整群并为一个任务。沿原 SA11 已验证精确投递关系优先；对受控 Coding 目标，
只允许原始消息的精确回复链／话题命中唯一当前绑定。同话题存在多个不同目标时
不猜选，保留未归属事实；不伪造 Issue，不复活已停止目标，不进行 Router 评估。
SA11 需要原资源证明；已有 fixture Coding 目标不会仅凭群 ID 自动获得订阅权。

读取前和实际发送前重查 Source Receive、identity、policy、canonical head、正文 TTL、
当前角色及精确目标。context_only、CI、机器人、工具日志、引用和模型建议均为数据。
限制只有当前已验证 human + task_controller + 精确 Ticket membership，或当前 Owner
的有类型请求能产生。撤权提交后不得使用旧正文；权限恢复不能恢复已失效工作视图。

首个受控 Source 控制语法只识别独立完整规范化文本“不要改数据库”；它映射到
服务端 profile 预先冻结的 database_write_paths／database_commands deny 集。
没有明确映射就 blocked，不猜测路径或调用模型解析权限。精确路径例子为 src/db.py
或 src/db，不按文件名子串决定权限。原 SA13 范围仍只改已有纯函数文件，不执行 SQL。
其他文本按原上下文处理，不能作为任意 Shell 或权限命令。结构化 Owner 入口可提交
精确 deny 集；只能并集收紧，不能清空、替换、换目标、授网络或追加付款。

## 4. 安全点与旧回复

新 context／restriction／invalidation 在接收事务中提升 input_revision；测试进度不
增加用户输入版本。新输入到达就使旧视图无法启动下一模型／工具／命令，不能等
消费完成才生效。安全点重查所有当前事实，形成不可变 checkpoint，原子标记已应用
输入与消费序号。无有效新输入不唤醒付费步骤；幂等重入不重复消费或重复派发。

应用新限制只收紧原 Mandate；原目标和 H11 TaskBinding.objective 保持不变。模型
请求、工具、文件编辑、实际容器启动及结果提交都绑定当时 checkpoint／input_revision。
after_model 发现有新输入时丢弃完整旧回复及全部工具调用。原编辑前／命令派发前
还有独立当前检查，防止把安全点当长期授权。hash／revision／epoch 不符明确阻止。

PG 不包含文件／网络／容器 IO。原 atomic 文件编辑开始后完成原 pre/postimage 和
实际树事实；新限制在下一操作生效，不假装撤销已发生的写。已发模型真实结算，
Source 删除／撤权只阻止重用正文和继续执行，不退款、不抹账、不伪报 Task 成功。

## 5. 可查询和取消的原 Job

每个 Job 一对一包装原 sa_coding_operations 和原 sa_scheduler_operations(kind=job)，
绑定相同 Assignment／Session／epoch／请求 hash／树／环境。不得接受客户端 argv、
随机 Broker.execute、宿主执行、临时文件进程表或另一个进程服务。
Job 状态 prepared → running → stop_requested／unknown／known；unknown 只能由原
可信回执进入 known。known 包含原 result_code、receipt hash 和 observed_at。
cancel 是持久停止请求，不等同于实际 stopped；未证明静默不得释放原槽位。

长命令的启动、检查、停止仍经 SA12 bound driver 和精确容器名称／标签。慢检查后
实际启动前重读原权限；等待和轮询期间不占 Engine 事务或长期全局 mutex。
进度只唤醒读侧状态和下一个安全点，不能授予执行或自动创建下一模型调用。

受控模型新增显式 start／handle 端口，把短派发与长等待分开：最终授权只保护实际
派发，等待响应期间释放 Engine mutex，允许控制输入／撤权提交；响应按原预占结算。
不把同步 complete 任意包线程并宣称已解决提交后发送窗口；真正发送仍须守卫。

## 6. 隐私、撤权与恢复

0047 前向增加原 Source policy／identity／role_binding／task_controller／ingress control／
canonical input／正文删除的同事务 sticky fencing。撤销后恢复权限，旧输入也不复活。
失效持久追加最小无正文事实、请求停止原在途 Job，并清除受影响的原 Coding 密文；
原 financial usage／unknown／原业务结果不修改。触发器只做 PG 操作，不停止容器或 IO。

每个原 Coding 操作有不可变 operation_view，绑定 async checkpoint 和所有已应用
Source 引用。原加密模型响应／checkpoint 的 TTL 取这些依赖和原到期的最短值，
每次解密重查当前 Source 权限／正文 TTL。权限恢复也不能恢复已经 tombstone 的密文。
Source plaintext 不进入新六表、日志或 H09 文件状态。

原 Engine recover 先暂停原 Run、推进 epoch、处理原 usage 和 SA12／SA13 恢复，
随后 SourceAsync 在同事务保留失效／未消费事实和 unknown Job。重启不恢复旧执行权，
不自动重发模型、文件操作或命令；实际容器只按原 bound 资源只读核查／受控停止。
重建 Python 唤醒信号不改变 PG 消费游标。未有可信结果时明确 unknown。

## 7. 正式共享范围与验证

拟编号 0047，六表／函数／触发器仅在本 SA 副本归 SA 独占；旧 43 SQL 不变，0045
仍归 Harness。最小共享补丁只有 Engine 组合／recover、API 注册、Ingress accepted_conn。
所有 SQL／补丁／before-after hash 进入本目录。§38 仅确认 SA13，必须新确认后才将
0047 复制进 migrations 或应用共享补丁。SQL 草案可在隔离 PG 做安装和草案检查。

私有 SourceAsync domain／rules／service／API／runtime／payload adapter 及测试归 SA；
原 SA13 私有 source_coding.py／runtime／journal 只增加管理模式、guard 和 TTL 对接。
不改原 Engine 财务算法、Harness 固定 kernel／contracts／Broker／Workspace／Verifier。
如需要增加共享文件或改变 SQL 作用范围，先补充提案和协调，不能暗扩。

正式检查覆盖 E09／E16／E20：真实 PG／Docker 测试中途 Source 控制，角色撤权与恢复，
最后实际派发窗口，新旧模型回复，编辑原子收尾，费用回执，双请求幂等／CAS／回滚，
旧 Worker／epoch，Source 编辑删除与最短 TTL，原 Job 进度／取消／未知对账，实际
SIGKILL 恢复。API 检查全部 12 路由及 Owner／Worker 分权和无正文元数据。

首次正式检查注册 make verify TASK=SAV1-14 后执行；纯契约、SQL 安装不能代替整卡。
每轮检查前提交不可变候选和源码归档，记录 SHA／命令／JUnit／退出码。失败修复后
仅重跑原失败用例／目标，保留原通过与失败；不拼称最终版本单轮全量通过。
SA14 未通过独立验收前不启动 SA15。
