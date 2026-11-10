# HV4-H01 → Super-Agent 协作交接

- 交接日期：2026-11-10
- 交接方：Harness（`/home/imini/magr-harness`）
- 接收方：Super-Agent（`/home/imini/magr-super-agent`，只读同步）
- 对应请求：`super-agent-v1/COORDINATION.md`「向 Harness 请求的 H01 交付」
- 本轮触发：`super-agent-v1/H01_REVIEW_FEEDBACK.md` 四项反馈
- 当前状态：**修复已交付，等待 Super-Agent 复验接收**；旧提交 `e1b3214` 的独立探针
  失败记录保留，不改写为 PASS。

## 1. 阅读与确认

已完整重读 H01 任务书（`TASKS.md#hv4-h01` 及 ROADMAP/ACCEPTANCE/plan/sources）和
本轮评审反馈。指纹是读取时的版本；Harness 状态更新会改变工作树文件。

| 文件 | 读取时间（UTC） | sha256 |
| --- | --- | --- |
| `docs/harness-v4/TASKS.md` | 2026-11-10T13:02:17Z | `335c7ff215db54c567e1b2eef2f395db850868fb4013897fa859f3c3d8d41c2a` |
| `docs/harness-v4/ROADMAP.md` | 2026-11-10T13:02:17Z | `d6696778d6983fe70ade6f2109f58748903b281b40d70f940ef349e9fff69f3a` |
| `docs/harness-v4/ACCEPTANCE.md` | 2026-11-10T13:02:17Z | `e1035c8825171ac1ac36c628ed5da7e12620eb761a152e28061d4aeaadb5d28a` |
| `docs/harness-v4/plan.json` | 2026-11-10T13:02:17Z | `58ac3aed5661e8f6efa187b16a62c889e0ffe12c03765ff956dda90d04695653` |
| `docs/harness-v4/sources.json` | 2026-11-10T13:02:17Z | `2fc881ba53204f8b7c93943d2f06068472d4b9a3d5f1fcb9f80d20f0dbcb34b6` |
| `super-agent-v1/COORDINATION.md` | 2026-11-10T13:02:17Z | `49cb00968a10380fc4456c5315756ee6f6429968911242b5bd54321b6060ec26` |
| `super-agent-v1/H01_REVIEW_FEEDBACK.md` | 2026-11-10T13:29:48Z | `b01443e546625b86faf8448ad66ac26c6b6a46daaf702f65eb8dbd233c1b2740` |
| 外部探针 `probe.py` | 2026-11-10T13:29:48Z | `b12d91dc92d3c1bcab8798d253582ca7467fa68083b94e7037356194333eddc1` |
| 外部探针 `result.json` | 2026-11-10T13:29:48Z | `8108b93e4ecfe08e86d8c73deb70cc9e23a8506d6a7e7f23aafb5e163d63cfd0` |

确认协作约定：双方不改对方活动工作树；共享 Engine/API/预算/迁移变更先协调；
每卡实施前完整阅读任务书；历史 FAIL/NOT_RUN 原样保留。本条为实际对话确认，
只实施一次；此前 CLI 返回 0 但未见送达的内容不作为已交付依据。

## 2. 交付状态

| 交付项 | 状态 | 路径 |
| --- | --- | --- |
| 固定修复提交 | `FIX_COMMIT_PENDING`（本地 `main`，未推送） | 见 `accept/HV4-H01/accept.json` |
| 旧提交 | `e1b3214`：独立探针 2 项 FAIL，**已被取代，不撤回历史记录** | 外部 `result.json` |
| 选型 ADR | DONE | `docs/harness-v4/adr/HV4-H01-deepseek-harness.md` |
| 版本化接口 | **`harness-contract-v1.1`**，FROZEN | `src/mgar_v3/harness/contracts.py` |
| 机器可读接口清单 | DONE | `experiments/harness-v4/harness-contract-v1.json` |
| 状态所有权映射 | DONE，已对齐真实 `run_events` schema | `docs/harness-v4/HARNESS_CONTRACTS.md` §2 |
| 受控接线样例 | DONE（测试适配器 + 真实本地测试） | `src/mgar_v3/harness/runtime.py`、`tests/test_harness_v4_h01.py` |
| 失败分支记录 | DSH `NOT_RUN_NO_NODE_RUNTIME`；旧 H01 探针 FAIL 保留 | ADR §3.2、accept.json |
| 真实验收证据 | 18 项 H01 测试、10 项 H00、226 项核心回归、复跑外部探针 PASS | `evidence/harness-v4/HV4-H01/` |

工程门与 live 门分开：H01 只声明契约与确定性参考实现；`live_model_gate`、
`live_agent_gate`、`live_publication_gate` 均为 `NOT_RUN`。

## 3. 评审反馈逐项回应

### 3.1 事件游标与步骤字段映射到真实 schema（已修复）

反馈：`EventEnvelope`/文档曾写 `app.run_events.seq` 和 `operation_id` 列，但真实列是
`event_id bigserial`、`run_id`、`kind`、`safe_payload`、`created_at`。

修复：

- `EventEnvelope.cursor` 明确映射 `app.run_events.event_id`（`Engine.events()` 按
  `event_id > after` 读取）。
- `envelope_id`、`operation_id`、`execution_epoch`、`revision`、`payload` 统一放入
  `safe_payload`，schema `harness-event-v1`，由
  `EventEnvelope.to_run_event_safe_payload()` 生成。
- 字段 `event_id` 更名为 `envelope_id`，避免与数据库 bigserial 列混淆。
- `STATE_OWNERSHIP` 与 `harness-contract-v1.json` 已同步；H01 不修改迁移、不新建
  事件表，持久适配在 H09 前向迁移交付。
- 新增 `test_event_envelope_maps_to_the_real_run_events_schema`，直接断言
  `0001_coordination.sql` 的 `event_id bigserial`/`safe_payload jsonb` 与安全载荷结构。

### 3.2 SA00 与真实自动修复的前置分开（已修正）

反馈：旧交接把 SA00 限制到 H02–H07/H11 之后，扩大了 SAV1-00 的 DAG。

修正：**SA00 只依赖 H00/H01**。H01 修复复验后，SA00 可完成来源、范围、复用审计、
ADR 与验收协议冻结。真实自动修复不在 SA00 范围内，等待 H11/H12 及所需工作区、
Broker、命令与编码能力验收；这不作为 SA00 的完成前置，也不提前开放修复。

### 3.3 actor 是受信内部主体，不直接承载平台 opaque ID（已修复）

反馈：`TaskBinding.actor` 曾使用 ASCII 正则并 `lower()`；飞书 ActorRef 可能区分
大小写且需要安装/租户范围。

修复：

- `actor` 不再 `lower()`，`_authorize` 使用精确比较；大小写不同即
  `HARNESS_ACTOR_MISMATCH`。
- `actor` 定义为 SA02 身份映射产出的受信内部 Engine principal，不是原始平台 ID。
- 原始 `SourceActorRef` 由身份服务单独保存，保留原大小写与安装/租户范围；禁止从
  正文/模型推断映射。
- 新增 `test_actor_is_a_case_sensitive_internal_principal`。
- 若未来 v1 需要直接承载原始平台 ID，必须另立字段与校验规则并升契约版本。

### 3.4 只读许可与未声明工具的默认拒绝缺口（已修复）

反馈：对 `e1b3214` 的隔离探针发现：

- Registry 声明 `custom_write` 为 write、Grant 的 `write_paths` 为空，handler 仍被调用 1 次。
- Model 返回 Registry 未声明的 `custom_write`，handler 同样被调用 1 次。

根因：`_precheck` 只校验四个固定工具名，其他名称返回 `None`；随后直接调用
可替换的 `Registry.invoke`。

修复：

- runtime 在每次 Run 开始时快照 `ToolRegistry.specs()`，建立已声明工具表。
- 未声明工具在 handler 之前直接 `denied`，事件带
  `HARNESS_TOOL_UNDECLARED`。
- 已声明工具按 `ToolSpec.capability` 做通用校验：
  - read：必须有 `path` 或 `edits[].path`，逐路径 `grant.can_read`。
  - write：必须有 `path` 或 `edits[].path`，逐路径 `grant.can_write`；无可识别
    路径时默认拒绝。
  - execute：必须有 `argv`，并通过 `grant.can_execute`。
- 不再仅凭固定工具名放行；H14 的通用扩展也必须声明 capability 与可校验参数。
- 新增 3 项真正负向回归：
  - `test_undeclared_tool_is_denied_before_the_handler` → handler 0 次；
  - `test_declared_write_tool_is_denied_under_a_read_only_grant` → handler 0 次；
  - `test_declared_write_tool_is_dispatched_only_with_a_matching_grant` → 授权匹配时
    handler 1 次，证明扩展工具仍可安全接入。
- 使用外部 `probe.py` 在 Harness 工作树复跑：两个 case 均为
  `handler_invocations=0`、`runtime_status=denied`、`conformance=PASS`。

### 3.5 核心回归旧竞态：`finish_success` 两事务窗口（补充定位，已修复）

反馈补充定位：`test_agent_page.py` 的 upgraded 用例在 run 已 `succeeded` 后仍看到
`stop_reason=None`。根因是 `repository_analysis/store.py::finish_success` 先由
`engine.commit_report()` 提交 run 终态，再用第二个事务写
`repo_analysis_runs.stop_reason/report_hash`；读者可能落在两个事务之间。

修复（最小原子性改动）：

- `Engine.commit_report()` / `commit_prepared_report()` 新增可选
  `repo_analysis_stop_reason`；在同一个 Engine 事务内写入
  `app.repo_analysis_runs.stop_reason` 与 `report_hash`。
- `RepositoryAnalysisStore.finish_success()` 只调用一次 `commit_report`，删除第二个
  更新事务；返回值仍从 `spec()` 读取。
- 未改变 run 状态机、迁移、预算或发布语义；其他 `commit_report` 调用不传该参数，
  行为不变。
- 新增确定性回归
  `tests/test_v32_repository_analysis_db.py::test_finish_success_commits_stop_reason_with_run_status`：
  在 `finish_success` 提交后、`spec()` 读取前用事件阻塞，再从独立连接读取
  `app.runs` 与 `app.repo_analysis_runs`；断言 `status=succeeded` 时
  `stop_reason=complete` 且 `report_hash` 已存在。修复前该断言会稳定看到
  `stop_reason=None`。
- 旧失败日志保留在 `evidence/harness-v4/HV4-H01/h01-review-fix-attempt1/`；
  修复后核心回归 226 passed（含新增 DB 原子性用例）。

## 4. 接口路径、版本与冻结状态

- 契约版本：**`harness-contract-v1.1`**（v1 在 `e1b3214` 的评审失败保留）
- 包路径：`src/mgar_v3/harness/`
- 入口：`mgar_v3.harness`（`__init__.py` 导出冻结类型与参考实现）
- 契约模块：`src/mgar_v3/harness/contracts.py`
- 参考组合：`src/mgar_v3/harness/runtime.py` 的 `HarnessRuntime`
- 本地适配器：`src/mgar_v3/harness/adapters.py`
- 旧服务适配：`src/mgar_v3/harness/legacy.py`

### 4.1 冻结接口

`TaskBinding`、`CapabilityGrant`、`ModelAdapter`、`ToolRegistry`、
`WorkspaceProvider`、`ExecutionBroker`、`ContextManager`、`Verifier`、
`EventSink`、`BudgetLedger`。

方法签名与执行顺序见 `HARNESS_CONTRACTS.md` §1/§3。v1.1 的破坏性变化：
`EventEnvelope.event_id` → `envelope_id`；`cursor` 的数据库含义明确化；actor 精确
比较。Super-Agent 应以 v1.1 重新校验，不接受 v1 的旧断言。

### 4.2 建议接口（未冻结）

流式模型 delta、命令 Job 的 run/poll/cancel、持久 inbox/安全点、上下文压缩与
分页、崩溃恢复/fencing、远程 Runner/多用户、插件/MCP 扩展。计划卡分别为
H02、H06、H09、H10、H11、H15–H16、H14。

## 5. Mandate → Task/Run 映射

字段级映射见 `HARNESS_CONTRACTS.md` §4。要点：

- actor 是受信内部 Engine principal，大小写敏感，必须与 Grant 精确一致；原始
  `SourceActorRef` 单独保存，不 lower、不从正文/模型推断。
- repository、workspace 必须与 Grant 一致，否则 `403`；repository 小写规范化。
- `base_commit` 为 40 位 SHA；工作区快照必须与该 commit 对应。
- `acceptance_revision` 与 `ContextView.revision` 绑定目标版本；目标变化必须递增。
- `execution_epoch` 用于旧 Worker fencing；H01 只定义字段，真正 fencing 在 H11。
- 预算：`BudgetLedger.reserve` 先于模型/工具调用；`settle` 按真实 usage；
  `mark_unknown` 保留预占并禁止盲重试。
- 未知模型结果：`UnknownModelOutcome` → `HarnessResult.status = "unknown"`。
- 最终完成：Verifier 回执 + Engine 事务；Harness `verified` 不是任务完成。

## 6. 能力边界与依赖

| 能力 | H01 状态 | 说明 |
| --- | --- | --- |
| 模型/工具/工作区/执行/上下文/验证/事件契约 | 提供 | v1.1 冻结 |
| 确定性参考回路（读→改→真实测试） | 提供 | 本地文件 + 真实 Python 子进程 |
| 旧只读分析适配 | 提供薄适配 | `LegacyAnalysisAdapter`，无写/执行方法 |
| 旧修复器适配 | 提供薄委托 | `LegacyRepairAdapter` → `RepairAgent.run` |
| 流式模型协议/第二模型供应商 | NOT_AVAILABLE | H02 |
| 本机/GitHub 工作区保护、dirty 处理 | NOT_AVAILABLE | H03 |
| 统一能力 Broker 与容器隔离 | NOT_AVAILABLE | H04；H01 只有本地一次性 Broker |
| 增量编辑/多文件原子补丁 | NOT_AVAILABLE | H05；H01 只有带 pre-image 的整文件编辑 |
| 命令 Job 启动/查询/取消 | NOT_AVAILABLE | H06；H01 只有一次性 `execute` |
| 统一编码循环/多类型验收 | NOT_AVAILABLE | H07；H01 是契约参考回路 |
| 持久 inbox/运行中交互 | NOT_AVAILABLE | H09 |
| 崩溃恢复/fencing | NOT_AVAILABLE | H11 |
| 真实模型/Agent/发布门 | NOT_RUN | H12 才有真实评测 |

**SA00 依赖**：仅依赖 H00/H01。H01 修复复验后，SA00 可完成范围、复用审计、ADR
与验收协议冻结；真实自动修复等待 H11/H12 及所需工作区/Broker/编码能力验收，
不属于 SA00 的 DAG，也不应提前开放。

## 7. Diff/tree、验证回执与 Review Bundle 版本绑定

- `WorkspaceSnapshot.tree_hash` 是模型输入、工具执行与验证共用的版本。
- `ExecutionReceipt` 记录 `tree_hash`、argv、cwd、退出码、测试数、日志 hash 和
  环境 hash；Verifier 只接受与当前 tree 匹配的回执。
- 真实 Diff 由 `WorkspaceProvider.diff(tree_before)` 生成；H01 适配器已实现，
  生产级增量 Diff/原子多文件回滚由 H05 强化。
- Review Bundle 继续由现有服务生成与封存；H01 不创建、不批准、不发布。
  审批必须绑定 Diff/tree/目标版本；该失效语义在 H07/H11 验证。

## 8. 协作、迁移与命名空间

- 未修改 `/home/imini/magr-super-agent` 的任何文件；仅只读读取反馈、探针与结果。
- 未修改共享 Engine、API、预算表、数据库迁移或现有修复/发布语义。
- 未新增业务写者；`mgar_v3.harness` 只做组合与端口。
- 未新增付费模型调用、GitHub 写入或网络发布。
- 历史 V35-E02 `FAIL`、E01/E03/publication `NOT_RUN` 未改写。
- 迁移编号：Sources 迁移由 Codex 负责 `0030`；Harness 若需共享迁移，先与 Codex
  对齐编号，不直接占用 0030。H01 未新增迁移。
- ADR 命名空间：Harness 使用 `docs/harness-v4/adr/HV4-Hxx-*.md`；Super-Agent 使用
  其轨道内命名空间，避免文件名碰撞。

## 9. 集成复验要求（Super-Agent 侧）

1. 以 `harness-contract-v1.json` 的 **v1.1** 校验接口名/方法；不要接受 v1 的旧
   `event_id` 字段或 lower 化 actor 断言。
2. 在 Super-Agent 自己的数据库/Redis/临时目录运行集成验证；H01 的证据只覆盖
   Harness 工作树。
3. 复跑外部 `probe.py`，两个 case 应为 handler 0 次、status `denied`。
4. 若需要新增字段或改变状态语义，先提出契约变更（版本、受影响卡、兼容方法），
   不能在 Super-Agent 内私建第二个 Harness 状态机。
5. 只有复验通过后才接收 H01；本交接不代替 Super-Agent 的独立复验。

## 10. 下一步

- Harness：等待 Super-Agent 复验 H01；收到接收结论后再启动 H02。
- Super-Agent：按 H00/H01 前置完成 SA00 冻结，不等待 H11/H12。
- DSH 后端：保持 `NOT_ADOPTED_H01` / `NOT_RUN_NO_NODE_RUNTIME`，在 H12/H14 条件
  具备时重新评估。
