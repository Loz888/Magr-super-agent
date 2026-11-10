# ADR HV4-H01：Harness 接口与 DeepSeek Harness 复用选型

- 状态：**Accepted**
- 日期：2026-11-10
- 决策卡：`HV4-H01`
- 契约版本：`harness-contract-v1.1`
- 关联：`docs/harness-v4/HARNESS_CONTRACTS.md`、`experiments/harness-v4/harness-contract-v1.json`

## 1. 背景

H01 需要在不新增业务写者的前提下，为模型、工具、工作区、执行、上下文、验证和
事件定义最小公共接口，并验证是否可以直接采用 DeepSeek Harness（DSH）作为执行
后端。任务书要求最多投入 2 人日，若 DSH 不满足契约则明确记录缺口并选择 Python
路线，不阻塞后续卡。

现有权威仍是 Engine/PostgreSQL：Thread、Run、`execution_epoch`、预算预占/结算、
Finding、Approval、Action/Outbox 与 Review Bundle 已有明确归属。H01 不复制这些
状态，只增加组合层。

## 2. 决策

1. **采用路线 A：现有 Python 内核加公共端口。** 新建 `src/mgar_v3/harness/`，
   定义 `TaskBinding`、`CapabilityGrant`、`ModelAdapter`、`ToolRegistry`、
   `WorkspaceProvider`、`ExecutionBroker`、`ContextManager`、`Verifier`、
   `EventSink`、`BudgetLedger` 的 v1 契约。
2. **不将 DSH 作为当前执行后端。** DSH 保持为 H12/H14 的可选对照后端，重新评估
   前不得引入第二套默认 Agent Loop 或独立业务裁决。
3. **固定执行顺序。** 每次模型调用先 `BudgetLedger.reserve`，调用后 `settle`；
   结果未知时 `mark_unknown` 保留预占并禁止自动重试。每次工具调用先由 runtime
   做权限预检，再由 Workspace/ExecutionBroker 在执行边界复核。
4. **完成结论只来自 Verifier 回执。** Harness 结果中的 `verified` 只是组合层结果，
   最终业务完成仍必须由 Engine 事务和现有审批/发布路径确认。

## 3. DSH 复用验证记录

### 3.1 已完成的只读核验

| 项目 | 结果 |
| --- | --- |
| 固定版本文档 | `deepseek-ai/deepseek-harness@fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`，版本与 sha256 记录在 `sources.json` |
| 架构文档 | Cordis 插件树；model adapter、tool registry、session log、agent loop 均可替换；工具管线提供 `tools/pre-execute`、monotonic guards、`tools/execute`、`tools/post-execute` |
| 会话模型 | 追加式 SessionEvent 日志；turn/step 事件；工具调用/结果持久化 |
| 执行边界 | filesystem 与 subprocess provider 共享同一执行世界；策略/审批在工具管线前置 |

这些设计与本卡的 `ToolRegistry`、`EventSink`、`WorkspaceProvider`、
`ExecutionBroker` 方向一致，说明 DSH 的概念模型可映射。

### 3.2 未完成/不满足的部分

| 契约要求 | DSH 核验状态 | 结论 |
| --- | --- | --- |
| 每次模型调用前接入现有 Engine 预算预占/结算 | 公开文档未给出与外部账本的预算 seam；需实测 | 未验证 |
| 工具/进程全部经过现有能力许可入口 | 有 `tools/pre-execute` 与 guarded pipeline，但需把现有 `CapabilityGrant` 接入插件 | 未验证 |
| DSH 会话投影到 Thread/Run/epoch，且不产生第二裁决 | 有 SessionEvent 与 turn/step 概念；映射代码不存在 | 未验证 |
| 取消、失联、重启恢复 | 文档描述 live cancellation 与 session log，但未与本项目恢复协议联调 | 未验证 |
| 凭据边界 | DSH 的 settings/credentials 插件与现有 Broker 不一致 | 未验证 |
| 运行时可用性 | 本机 `node` 不存在、`pnpm` 不存在；仅 `npm` 可用 | 无法启动受控接线 |

本机探测命令与结果：

```text
$ node --version
bash: node: command not found
$ pnpm --version
bash: pnpm: command not found
```

因此 H01 无法执行 DSH 的受控模型接线，也无法诚实地给出 DSH 的预算/权限/取消
实测结论。该分支记录为 **NOT_RUN**，而不是 FAIL 或 PASS。

### 3.3 成本比较

| 维度 | 路线 A：Python 内核 + 端口 | 路线 B：DSH 执行后端 |
| --- | --- | --- |
| 现有 Engine/预算/审批复用 | 直接 | 需要通过插件/代理重新接线 |
| 工作区与沙箱 | 复用现有 R03/R5 与 Worktree | 需把 provider 映射到现有隔离边界 |
| 模型协议 | 复用 `repository_analysis/model.py` 与后续 H02 适配器 | 需 Node 运行时、SDK 版本锁定和传输桥 |
| 状态投影 | 直接使用 Thread/Run/epoch | 需把 SessionEvent 单向投影到现有账本，禁止反向裁决 |
| 恢复/取消 | 复用现有 checkpoint/epoch | 需验证 DSH cancellation 与重启语义 |
| 当前可验证性 | 本机可运行 | 本机无 Node，NOT_RUN |
| 维护面 | 一个 Python 默认内核 | 两套默认执行内核风险 |

路线 B 的潜在收益是成熟的插件生态与多入口，但在预算、状态投影、凭据和恢复四项
没有实测前，不足以替换现有权威结构。

## 4. 后果

- H02–H18 默认在 `mgar_v3.harness` 契约上实现；H02 的模型适配器不依赖 DSH。
- H12 若条件具备，可用同一 DeepSeek 模型做 MAGR 与 DSH 对照；没有 Node 运行时或
  授权时继续记 `NOT_RUN`，不影响 H02–H11。
- 任何未来采用 DSH 的提案必须先满足：预算预占/结算可拦截、工具能力入口不可绕过、
  状态可投影且无第二裁决、取消/恢复可复现、版本与许可证固定。
- H01 的 `NOT_RUN` 不会被后续文档改写成 PASS；新结果必须追加新的 attempt 记录。

## 5. 参考

- `docs/harness-v4/sources.json`（DSH README/architecture/tool pipeline/SAFETY 的固定
  commit 与 sha256）
- `docs/harness-v4/HARNESS_CONTRACTS.md`（v1 接口与状态映射）
- `experiments/harness-v4/harness-contract-v1.json`（机器可读冻结清单）
- `tests/test_harness_v4_h01.py`（参考实现与负向契约测试）
