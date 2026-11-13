# SA13 具体接线范围提案

状态：DRAFT_NOT_APPLIED。SA12 已由用户接受独立闭环；本卡仍 IN_PROGRESS。
本稿、SQL 与补丁不代表接线、运行或能力门已经通过。

固定依赖以 harness-imports.json 为准：H11 d3f3457 的工程／恢复范围有独立证据；
H12 在固定 88c75b8 的 live_agent、MVP 门为 FAIL。环境恢复或旧模型连通 PASS
不改变这一结论。先实现并验证受控工程桥；真实 Coding、付费和外发持续拒绝。
不导入完整 H11/H12 Store、Service、持久钱包或独立任务创建器。

## 原权威与新 Mandate

原 Engine 的 advisory lock、mutex、同一 PG 连接和原 Thread/Ticket/Run/Mandate
继续是唯一业务权威。source-coding-mandate-v1 是原 Mandate.scope 的版本化完整模型，
不是第二个任务或通用提升授权接口。新图标记 source-coding-v1，问题列只写固定元数据，
Source 正文和模型历史不进入旧 question、trigger payload 或 run_inputs。

Owner 的 admit 只接受已经存在的此类新 Mandate／原 Run；旧调查、readonly 或已有
旧执行事实直接拒绝。若受信来源工厂创建新目标，它须在原连接同一事务中完成来源准入、
目标去重、新原表记录、Coding binding 和 SA12 入队，不能留下旧 supervisor 可抢取的
未受管 queued Run。不能复制 HarnessStore.create，也不能更换旧 Ticket 的 Mandate
使历史 Run 读取新权限。GitHub 明确未认领资源可复用原关联 winner factory；飞书
无 native proof 不满足该旧工厂时，使用预建且明确审阅的新 Coding 目标准入，不虚构 Issue。
完整自动建工与任意无资源群话题扩展须另外验明，不能由本提案推定。

Coding 绑定冻结原五个 ID、Source canonical 引用、身份／principal／policy 版本、
仓库不可变 ID、上游 commit/tree、本地物化 commit/tree、路径、命令、环境、预算／
价格引用、上下文与验收 hash、profile、签署人和期限。上游 SHA 不被本地新 commit 冒充。
Source、策略、身份、目标、Session、Assignment、lease、epoch 和代码树在每个边界重查。
receive、model input、执行和代码披露分别鉴权；上下文或正文不产生写许可。

## 固定内核与资源适配

只取 H11 的 coding_loop.py、long_context.py、adapters.py、incremental.py 固定 blob。
adapters 原字节放到新 source_coding_adapters.py，旧 adapters.py 保持，避免改变 H01
两参数 reference registry。Source 必须显式提供严格 authority，不能选择宿主
LocalExecutionBroker、LocalWorkspace 或 reference 无权限路径。

contracts 补丁保留 content 必填、原默认值与 SA12 execution_epoch；添加必要 provenance
类型和默认 None。新增 provenance 的 null 序列化差异须初次集成验证。replacement-only
输入全拒绝（包括一般 Mapping 的 null／空列表），增量 helper 失败关闭，不修改旧 R5
worktree。Source 工具 schema 只声明 full-content，不能悄悄丢弃 supplied replacements。

H03 façade 要提供受管 resolve、把 snapshot ID 映射到 Assignment workspace UUID，
并将 diff(tree_before) 适配为真实 WorkspaceDiff；原 H03 的 path-filtered 字符串 diff
不直接充当该协议。首片仅现有普通文件的 fixture bugfix；新建文件、通用 feature、
refactor 和 run_app 均不由本提案开启。多文件写入必须记 pre/postimage 意图和恢复事实，
不把逐个 O_TRUNC 当成原子提交。

所有新资源／workspace façade 统一 mutex → 私有 workspace lock 顺序；不直接调用锁序
相反的 H11 RecoveryIO 实现。SQL 事务在文件／进程／模型 IO 前结束。模型与命令最终
派发跨实际入口保留原 mutex，慢 IO 后重查当前 PG 时间和撤权，参考 SA09-I01 和 SA12。
命令先记原 SA12 resource operation，确定 name/完整 owner labels，再用 bound launch；
不能直接调用随机身份的旧 Broker.execute。未知只查证，不重发；收尾仍由 SA12 可信
静默、pending jobs／operations 和 slot 事实约束。

裸 ExecutionReceipt／LocalVerifier 或 Worker 自报不能写 patch_verified 或业务成功。
Source 私有 journal 应核对原 Task/Assignment/session/epoch/operation、实际 H03 tree、
environment、profile、验收集和可信测试链。新 verification 仅是事实；内部提交须再次
检查当前权威，不调用会进入旧 GitHub Action 路径的 commit_report。

## 财务、内容与恢复

不使用 Harness PersistentBudgetLedger 新建额度。私有适配器只接原 usage_calls／
原结算和 SA07 组织／项目预算，工具和模型意图幂等绑定同一原 Run；unknown 继续占款。
本轮只有显式受控 provider，默认 None，不能由 fixture 预算或环境变量启动付款。
原 Engine／Router／Repository 财务函数和 SA09-I01 栅栏不改。

Source 正文按当前许可临时物化，Worker 不获得 Owner cookie 或安装 token。需要恢复的
模型响应／checkpoint 只能有限加密保存，期限不越当前 Source 绑定；每次解密重验权限，
撤权／到期不可读并可不可逆清空密文，保留最小水位。不能套用 H11 的普通明文 history
或任意 CAS 持久化。GitHub App token 只留在受信只读快照通道，真实平台读取本轮 NOT_RUN。

拟 0046 增六类事实：bindings、attempts、operations、payloads、verifications、events。
没有第二 Task、授权签发器或财务账本；model operation 只引用原 usage/SA07 预占。
原 42 SQL 不变，0045 继续归 Harness。安装前需对齐编号与此具体接口／补丁范围。

## 六个 metadata 接口

前缀 `/api/v3/source-coding`。Owner 用当前 cookie、Origin/CSRF 和事务内会话复查；
Worker 用独立可撤销 bearer 与当前 Assignment 精确绑定。响应 no-store，无正文或宿主路径。

| 主体 | 方法 | 路径 |
| --- | --- | --- |
| Owner | GET | /capabilities |
| Owner | POST | /admissions |
| Owner | GET | /bindings/{coding_id} |
| Owner | POST | /bindings/{coding_id}/cancel |
| Worker | POST | /worker/{coding_id}/run |
| Worker | GET | /worker/{coding_id}/state |

请求不包含命令、host path、provider factory、token、付款开关、Task 成功或权限自证。
Owner admission 不等于把旧调查 scope 改成可写。Server profile 固定内核／镜像／工具边界。
默认不启动 Worker、监督线程、模型或来源监听。

## 首次集成检查与交付边界

纯契约检查不能替代正式 SA13。接线确认后注册实际完整目标，覆盖真实 PG、隔离 Broker、
当前授权最后派发门、工作区多轮失败后修复、hash 绑定、原共享预算、unknown／崩溃恢复、
锁序竞争及当前 H01/H02/SA12 直接依赖。修复后只重跑失败项，保留所有原始结果。
旧公开只读入口、H03 与 SA12 语义均须保持；新 capability 不由旧检查通过推导。

真实来源、真实模型质量、真实编码、私有源码披露、发布和长期门另列；H12 的 FAIL
不改成 NOT_RUN 或 PASS。本卡未独立验收前不启动 SA14。
