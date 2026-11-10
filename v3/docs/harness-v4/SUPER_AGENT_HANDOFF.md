# HV4-H01 → Super-Agent 协作交接

- 交接日期：2026-11-10
- 交接方：Harness（`/home/imini/magr-harness`）
- 接收方：Super-Agent（`/home/imini/magr-super-agent`，只读同步）
- 对应请求：`super-agent-v1/COORDINATION.md`「向 Harness 请求的 H01 交付」

## 1. 阅读确认

已完整阅读以下文件。指纹是读取时的版本；后续 Harness 状态更新会改变工作树文件，
但本记录保留的是 H01 实施前的输入版本。

| 文件 | 读取时间（UTC） | sha256 |
| --- | --- | --- |
| `docs/harness-v4/TASKS.md` | 2026-11-10T13:02:17Z | `335c7ff215db54c567e1b2eef2f395db850868fb4013897fa859f3c3d8d41c2a` |
| `docs/harness-v4/ROADMAP.md` | 2026-11-10T13:02:17Z | `d6696778d6983fe70ade6f2109f58748903b281b40d70f940ef349e9fff69f3a` |
| `docs/harness-v4/ACCEPTANCE.md` | 2026-11-10T13:02:17Z | `e1035c8825171ac1ac36c628ed5da7e12620eb761a152e28061d4aeaadb5d28a` |
| `docs/harness-v4/plan.json` | 2026-11-10T13:02:17Z | `58ac3aed5661e8f6efa187b16a62c889e0ffe12c03765ff956dda90d04695653` |
| `docs/harness-v4/sources.json` | 2026-11-10T13:02:17Z | `2fc881ba53204f8b7c93943d2f06068472d4b9a3d5f1fcb9f80d20f0dbcb34b6` |
| `magr-super-agent/v3/docs/super-agent-v1/COORDINATION.md` | 2026-11-10T13:02:17Z | `49cb00968a10380fc4456c5315756ee6f6429968911242b5bd54321b6060ec26` |

确认协作约定：双方不改对方活动工作树；共享 Engine/API/预算/迁移变更先协调；
每卡实施前完整阅读任务书；历史 FAIL/NOT_RUN 原样保留。

## 2. 交付状态

| 交付项 | 状态 | 路径 |
| --- | --- | --- |
| 稳定提交 | 见 `docs/harness-v4/accept/HV4-H01/accept.json` 的 `commit` / `result_commit` | 本地 `main`，未推送 |
| 选型 ADR | DONE | `docs/harness-v4/adr/HV4-H01-deepseek-harness.md` |
| 版本化接口 | DONE，`harness-contract-v1`，FROZEN | `src/mgar_v3/harness/contracts.py` |
| 机器可读接口清单 | DONE | `experiments/harness-v4/harness-contract-v1.json` |
| 状态所有权映射 | DONE | `docs/harness-v4/HARNESS_CONTRACTS.md` §2 |
| 受控接线样例 | DONE（测试适配器） | `src/mgar_v3/harness/runtime.py`、`tests/test_harness_v4_h01.py` |
| 失败分支记录 | DSH 接线 `NOT_RUN_NO_NODE_RUNTIME` | ADR §3.2、accept.json |
| 真实验收证据 | 见 `evidence/harness-v4/HV4-H01/` 与 accept.json | 本地证据目录 |

工程门与 live 门分开：H01 只声明契约与确定性参考实现；`live_model_gate`、
`live_agent_gate`、`live_publication_gate` 均为 `NOT_RUN`。

## 3. 接口路径、版本与冻结状态

- 契约版本：`harness-contract-v1`
- 包路径：`src/mgar_v3/harness/`
- 入口：`mgar_v3.harness`（`__init__.py` 导出冻结类型与参考实现）
- 契约模块：`src/mgar_v3/harness/contracts.py`
- 参考组合：`src/mgar_v3/harness/runtime.py` 的 `HarnessRuntime`
- 本地适配器：`src/mgar_v3/harness/adapters.py`
- 旧服务适配：`src/mgar_v3/harness/legacy.py`

### 3.1 冻结接口

`TaskBinding`、`CapabilityGrant`、`ModelAdapter`、`ToolRegistry`、
`WorkspaceProvider`、`ExecutionBroker`、`ContextManager`、`Verifier`、
`EventSink`、`BudgetLedger`。

方法签名与执行顺序见 `HARNESS_CONTRACTS.md` §1/§3。冻结含义：Super-Agent 可按
这些字段/方法设计依赖；破坏性改名必须升版本并说明受影响的 SA/HV4 卡。

### 3.2 建议接口（未冻结）

流式模型 delta、命令 Job 的 run/poll/cancel、持久 inbox/安全点、上下文压缩与
分页、崩溃恢复/fencing、远程 Runner/多用户、插件/MCP 扩展。计划卡分别为
H02、H06、H09、H10、H11、H15–H16、H14。

## 4. Mandate → Task/Run 映射

字段级映射见 `HARNESS_CONTRACTS.md` §4。要点：

- actor、repository、workspace 三者必须与 Grant 一致，否则 `403`。
- `base_commit` 为 40 位 SHA；工作区快照必须与该 commit 对应。
- `acceptance_revision` 与 `ContextView.revision` 绑定目标版本；目标变化必须递增。
- `execution_epoch` 用于旧 Worker fencing；H01 只定义字段，真正 fencing 在 H11。
- 预算：`BudgetLedger.reserve` 先于模型/工具调用；`settle` 按真实 usage；
  `mark_unknown` 保留预占并禁止盲重试。
- 未知模型结果：`UnknownModelOutcome` → `HarnessResult.status = "unknown"`。
- 最终完成：Verifier 回执 + Engine 事务；Harness `verified` 不是任务完成。

## 5. 能力边界（H01 完成时）

| 能力 | H01 状态 | 说明 |
| --- | --- | --- |
| 模型/工具/工作区/执行/上下文/验证/事件契约 | 提供 | v1 冻结 |
| 确定性参考回路（读→改→真实测试） | 提供 | 使用本地文件和真实 Python 子进程 |
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

**对 SA00 的约束**：可以使用 `harness-contract-v1` 做依赖设计与状态映射；不得据此
宣称真实自动修复已开放。真实修复链路至少需要 H03（工作区）、H04（Broker）、
H06（测试进程）、H07（编码循环）和 H11（恢复）完成，并通过各自验收。

## 6. Diff/tree、验证回执与 Review Bundle 版本绑定

- `WorkspaceSnapshot.tree_hash` 是模型输入、工具执行与验证共用的版本。
- `ExecutionReceipt` 记录 `tree_hash`、argv、cwd、退出码、测试数、日志 hash 和
  环境 hash；Verifier 只接受与当前 tree 匹配的回执。
- 真实 Diff 由 `WorkspaceProvider.diff(tree_before)` 生成；H01 适配器已实现，
  生产级增量 Diff/原子多文件回滚由 H05 强化。
- Review Bundle 继续由现有服务生成与封存；H01 不创建、不批准、不发布。
  审批必须绑定 Diff/tree/目标版本；该失效语义在 H07/H11 验证。

## 7. 协作约束遵守情况

- 未修改 `/home/imini/magr-super-agent` 的任何文件；仅只读读取 COORDINATION.md。
- 未修改共享 Engine、API、预算表、数据库迁移或现有修复/发布语义。
- 未新增业务写者；`mgar_v3.harness` 只做组合与端口。
- 未新增付费模型调用、GitHub 写入或网络发布。
- 历史 V35-E02 `FAIL`、E01/E03/publication `NOT_RUN` 未改写。

## 8. 集成复验要求（Super-Agent 侧）

1. 以 `harness-contract-v1.json` 校验接口名/方法，不要复制本仓库未提交文件。
2. 在 Super-Agent 自己的数据库/Redis/临时目录运行集成验证；H01 的证据只覆盖
   Harness 工作树。
3. 若需要新增字段或改变状态语义，先提出契约变更（版本、受影响卡、兼容方法），
   不能在 Super-Agent 内私建第二个 Harness 状态机。
4. H02–H07、H11 完成前，SA00 只做审计与需求准备，不宣称依赖满足。

## 9. 下一步

- Harness：H02 模型能力接口与通用工具调用。
- Super-Agent：SA00 可基于本交接与 `HARNESS_CONTRACTS.md` 做只读依赖准备。
- DSH 后端：保持 `NOT_ADOPTED_H01` / `NOT_RUN_NO_NODE_RUNTIME`，在 H12/H14 条件
  具备时重新评估。
