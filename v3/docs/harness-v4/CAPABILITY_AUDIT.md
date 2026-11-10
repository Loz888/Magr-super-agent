# Harness V4 能力审计（HV4-H00）

基线：`973994c`（`origin/main`，含 harness-v4 规划文档与近期 Agent UI 回归）。
应用基线：`1d07285aa27ece9bae4ccbedc3a77ce2fbcbaf3a`（规划文档记录的审计基线）。
本表只描述**当前代码事实**，不代表任何未来能力或门禁已通过。

状态口径：

- **首页已接入**：从首页/现有 Web 入口可以直接触发，且回执进入现有权威账本。
- **已有组件**：源码存在且有自己的测试，但尚未成为统一编码任务的默认路径。
- **未接线**：能力缺口明确，产品或服务层没有真实调用路径。
- **未实测**：组件或流程存在，但没有在声明范围内取得可复核的真实运行证据。

## 1. 能力清单

| 能力 | 当前事实 | 状态 | 主要证据 |
| --- | --- | --- | --- |
| 只读仓库分析（固定 Commit） | 可解析 Commit，读取/列目录/搜索/Issue/PR 六类只读工具 | 首页已接入 | `src/mgar_v3/repository_analysis/tools.py`、`agent.py` |
| 证据引用与追问 | Finding 绑定 `evidence_id` 与原文引用；追问新开 Run | 首页已接入 | `repository_analysis/service.py`、`workbench/service.py` |
| 只读分析预算 | 默认 6 次模型 / 20 次工具 / 60k 输入 token / $0.5 / 600s，硬上限 12/40 | 首页已接入 | `quality_eval/v35/tasks.py`、`coordination/engine.py` |
| PostgreSQL 唯一事实源 | Run/Ticket/Mandate/Budget/Usage/Action/Approval/Outbox 均由 Engine 事务写入 | 已有组件 | `coordination/engine.py`、`migrations/` |
| 内容寻址产物（CAS） | `.local/artifacts/<hash[:2]>/<hash>` 与 `app.artifacts` | 已有组件 | `artifacts/`、`coordination/engine.py` |
| Review Bundle / Action / Approval / Outbox | 固定入口、payload hash、审批失效与只读对账 | 已有组件 | `repair/publication.py`、`workbench/service.py` |
| SSE 游标与断线重连 | 分析 Run 有持久事件和游标；编码事件尚未扩展 | 已有组件 | `repository_analysis/streaming.py`、`api.py` |
| Checkpoint / resume | 分析 Run 可恢复；恢复未知结果时 fail-closed | 已有组件 | `repository_analysis/agent.py`、`coordination/engine.py` |
| 整文件补丁替换 | 修复器返回整文件内容，8 文件 / 400 行 / 65,536 字节上限 | 已有组件 | `repair/providers/structured.py`、`repair/worktree.py` |
| 固定 R03 沙箱测试执行 | Docker `network none`、非 root、只读根、隐藏测试同容器运行 | 已有组件 | `repair/sandbox_executor.py` |
| Fixture 修复 + 审批 + Draft PR | 仅 fixture 路径；live 修复返回 `LIVE_REPAIR_EXECUTION_NOT_ENABLED` | 已有组件 | `workbench/service.py` |
| 首页提交编码任务 | 首页只展示只读分析；没有选择项目并编辑/测试的入口 | 未接线 | `web/agent.js`、`api.py` |
| 增量编辑工具 | 没有按文件/片段 hash 定位的小范围替换协议 | 未接线 | `repair/providers/structured.py` |
| 命令 run/poll/cancel | 只有测试执行器，没有通用命令句柄与进程组管理 | 未接线 | `repair/sandbox_executor.py` |
| 应用启动与就绪探针 | 没有端口、就绪、停止和租约清理能力 | 未接线 | 无 |
| 审批收件箱与运行中补充指示 | 没有持久 inbox、ask_user、安全点重计划 | 未接线 | 无 |
| 实时工具事件与日志分页 | 没有 plan/tool/command/patch/test 事件与 CAS 分页 | 未接线 | `repository_analysis/streaming.py` |
| 长上下文压缩后按需重读 | 有 ContextBuilder/压缩，但没有可调用的分页/重读工具 | 未接线 | `context/`、`repository_analysis/memory_loop.py` |
| 真实模型完成读 → 改 → 测试 | V35-E02 四臂 `PATCH_TRUNCATED`，结论 `INCONCLUSIVE` | 未实测 | `docs/v35/cards/V35-E02.md`、`docs/v35/EVALUATION_INDEX.md` |
| 真实 GitHub 写（Draft PR） | V35-D01 记录 `live_publication_gate=NOT_RUN` | 未实测 | `docs/v35/accept/V35-D01/accept.json` |
| DeepSeek Harness 复用验证 | 规划已固定路线 A/B 与放行条件，尚无接线样例 | 未实测 | `docs/harness-v4/ROADMAP.md` |
| 多项目并发隔离 | 工作区/命令/额度隔离未实现 | 未接线 | 无 |
| Headless / CLI / SDK | CLI 有只读命令；统一会话、提交、恢复 API 未实现 | 未接线 | `cli.py`、`api.py` |

## 2. 结构化缺口

1. **输出协议与任务形态冲突**：整文件替换要求模型一次返回完整文件；大文件小修正在
   8192 输出 token 上限下必然截断。这是 H05 的硬前置，不是提示词问题。
2. **执行世界不统一**：只读分析读取固定快照，测试执行在临时 R03 容器；没有
   编辑、命令与服务共享的同一工作区版本。
3. **产品面只读**：首页、CLI 与 API 都围绕审计报告，编码任务的 Diff/命令/测试回执
   没有可展示的权威投影。
4. **恢复面只覆盖分析**：补丁、命令、进程与外部发布的中断恢复尚无统一矩阵。
5. **验证独立性与运行期隔离不足**：隐藏测试只在 Prompt 中剔除，运行期仍与公开测试
   同容器；模型新增测试不能替代独立判定器。

## 3. 与 H00 冻结内容的关系

- 任务类型契约见 [TASK_TYPES.md](TASK_TYPES.md) 与 `experiments/harness-v4/task-types.json`。
- 评分协议 v1 见 [SCORING_PROTOCOL.md](SCORING_PROTOCOL.md) 与
  `experiments/harness-v4/scoring-v1.json`。
- 24 个冻结任务见 `experiments/harness-v4/tasks/dev.jsonl` 与 `holdout.jsonl`。
- 基线回归与静态检查见 [BASELINE_REGRESSION.md](BASELINE_REGRESSION.md)。
- 机器可读基线见 `experiments/harness-v4/baseline.json`。

本审计不修改 V3.5 的历史 DONE/FAIL/NOT_RUN 记录，也不把规划文档的存在当作能力证明。
