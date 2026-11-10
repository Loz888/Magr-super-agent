# Harness V4 接口契约 v1（HV4-H01）

- 契约版本：`harness-contract-v1`，状态 **FROZEN**
- 机器可读清单：`experiments/harness-v4/harness-contract-v1.json`
- 代码位置：`src/mgar_v3/harness/`
- 选型 ADR：[HV4-H01-deepseek-harness.md](adr/HV4-H01-deepseek-harness.md)

本契约只定义组合层端口。Thread、Run、`execution_epoch`、预算、Finding、Approval、
Action/Outbox 与 Review Bundle 继续由现有 Engine/PostgreSQL 持有。

## 1. 接口与冻结状态

### 1.1 冻结接口

| 接口 | 方法 | 职责 | 执行边界 |
| --- | --- | --- | --- |
| `TaskBinding` | — | 绑定 task/thread/run/epoch、actor、repository、base commit、workspace、acceptance revision 与目标 | 只读快照 |
| `CapabilityGrant` | `can_read` / `can_write` / `can_execute` | 工作区/路径/命令/网络/时限授权 | runtime 预检 + Broker 复核 |
| `ModelAdapter` | `complete` | 一次模型调用，返回文字、工具调用、finish reason、usage | 调用前预占，调用后结算 |
| `ToolRegistry` | `specs` / `invoke` | 声明并执行内建工具 | 只能由 runtime 调度 |
| `WorkspaceProvider` | `snapshot` / `read` / `list_dir` / `apply` / `diff` | 版本化文件工作区 | 编辑需 pre-image/tree 校验 |
| `ExecutionBroker` | `execute` | 一次性命令/测试执行 | 校验 grant、cwd、argv、timeout |
| `ContextManager` | `build` | 生成上下文与 revision | 不授予权限 |
| `Verifier` | `verify` | 根据执行回执判定验收项 | 只认可匹配 tree 的回执 |
| `EventSink` | `emit` / `events` | 持久事件信封与游标 | 事件不替代业务状态 |
| `BudgetLedger` | `reserve` / `settle` / `mark_unknown` / `release` / `snapshot` | 预占优先的预算账本 | 模型/工具调用前强制 |

### 1.2 建议接口（尚未冻结）

| 能力 | 计划卡 | H01 状态 |
| --- | --- | --- |
| 流式模型 delta 与分片 JSON | H02 | 未实现 |
| 命令 Job 的 run/poll/cancel | H06 | 未实现；H01 只有一次性执行 |
| 持久 inbox、ask_user、安全点重计划 | H09 | 未实现 |
| 上下文压缩与分页工具 | H10 | 未实现 |
| 崩溃恢复、Worker fencing | H11 | 未实现 |
| 远程 Runner、多用户隔离 | H15–H16 | 未实现 |
| 插件/MCP 扩展接口 | H14 | 未实现 |

## 2. 状态所有权映射

| Harness 概念 | 现有权威 | 说明 |
| --- | --- | --- |
| 会话 | `app.threads` | Harness 不新建 Session 数据库 |
| 轮次/任务 | `app.runs` | 一次明确任务或追问绑定一个 Run |
| 步骤 | `run_events.operation_id` | Harness 事件信封携带 operation_id |
| 执行代 | `app.runs.execution_epoch` | 旧 epoch 不得继续编辑/执行 |
| 预算预占/结算 | Engine 模型/工具账本 | Harness 只调用入口，不自行记费用 |
| Finding | 现有 finding store | Harness 不改变生命周期 |
| Approval | 现有 approval record | 批准/拒绝不由 Harness 决定 |
| 任务状态 | Engine task transaction | Harness 结果不是任务完成 |
| 最终完成 | Engine 事务 + Verifier 回执 | 二者缺一不可 |
| 事件游标 | `app.run_events.seq` | Harness 信封可投影到该游标 |
| 工作区版本 | `WorkspaceSnapshot.tree_hash` | 模型/工具/验证共享同一版本 |
| Review Bundle | 现有 Review Bundle 服务 | Harness 不发布 |

Harness **不拥有**：任务状态、Finding 生命周期、审批决定、预算结算、发布、最终
业务完成。

## 3. 执行顺序（不可绕过）

```text
run_started
  -> BudgetLedger.reserve(model)
  -> ModelAdapter.complete
  -> BudgetLedger.settle | mark_unknown
  -> CapabilityGrant 预检（runtime）
  -> BudgetLedger.reserve(tool)
  -> ToolRegistry.invoke
       -> WorkspaceProvider 或 ExecutionBroker 边界复核
  -> BudgetLedger.settle(tool)
  -> Verifier.verify（匹配 tree 的执行回执）
  -> run_finished
```

约束：

1. 模型适配器只能拿到声明后的 `ToolSpec`，不能拿到 `ToolRegistry` 实例。
2. 工具适配器不能直接写业务账本；预算和权限只在 runtime/Broker 入口发生。
3. 未知模型结果保留预占，标记 `HARNESS_MODEL_UNKNOWN`，不得自动重试。
4. `verified` 只表示 Verifier 接受当前 `tree_hash`；不能直接改成 Engine 的任务完成。

## 4. Super-Agent Mandate 映射

Super-Agent 的 Source/策略生成 Mandate 后，按下表映射为现有 Task/Run 字段；H01
只冻结字段与语义，不实现 SAV1 的 Router/订阅。

| Mandate/任务输入 | Harness/Engine 字段 | 说明 |
| --- | --- | --- |
| actor / owner | `TaskBinding.actor`、`CapabilityGrant.actor` | 必须一致，否则 403 |
| scope / project | `TaskBinding.repository`、`WorkspaceSnapshot.workspace_id` | repository 与 workspace 绑定 |
| repository | `TaskBinding.repository` | 小写规范化 |
| base SHA | `TaskBinding.base_commit` | 40 位 commit；工作区快照必须与之对应 |
| task/run | `TaskBinding.task_id`、`thread_id`、`run_id` | 由现有 Task/Run 生成 |
| input revision | `TaskBinding.acceptance_revision`、`ContextView.revision` | 目标变化必须递增 |
| execution epoch | `TaskBinding.execution_epoch` | 旧 epoch 的 Worker 不得写回 |
| budget | `BudgetLimits` / `BudgetLedger` | 预占先于调用，结算按真实 usage |
| unknown outcome | `BudgetLedger.mark_unknown` | 保留预占，禁止盲目重试 |
| workspace | `WorkspaceSnapshot.tree_hash` | 编辑/执行/验证共用 |
| verification | `VerificationReceipt` | 只认可匹配 tree 的回执 |

## 5. 与旧只读分析和修复器的兼容

| 适配器 | 路径 | 边界 |
| --- | --- | --- |
| `LegacyAnalysisAdapter` | `src/mgar_v3/harness/legacy.py` | 只读；没有 apply/execute/approve 方法 |
| `LegacyRepairAdapter` | `src/mgar_v3/harness/legacy.py` | 只委托 `RepairAgent.run`，不新增发布或审批权 |

旧分析/修复的完整行为仍由既有 V32/V34/V35 测试覆盖；H01 只验证签名与委托边界。

## 6. 兼容边界（H01 完成时）

- H01 未修改 Engine、API、预算表、数据库迁移或现有修复/发布语义。
- 未新增 Model 供应商调用；`live_model_gate`、`live_agent_gate`、
  `live_publication_gate` 均为 `NOT_RUN`。
- 未提供 H03 的本机/GitHub 工作区保护、H04 的真实容器 Broker、H05 的增量编辑、
  H06 的命令 Job、H09 的持久 inbox、H11 的恢复/fencing。
- DSH 后端未接入；ADR 记录 `NOT_RUN_NO_NODE_RUNTIME`，不得在后续文档改写成 PASS。
