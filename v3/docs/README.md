# MAGR Super-Agent 文档导航

从[项目首页](../../README.md)开始，了解持续研发问题如何接续。各能力的实际版本、验证范围与未决事项见[当前状态](CURRENT_STATUS.md)；历史设计与实验保留当时的条件和结论。

## 了解与使用

- [项目说明：一个研发问题如何被接续](PROJECT_OVERVIEW.md)
- [应用启动与 WSL2](../README.md) · [单页 Agent 使用说明](agent-ui.md) · [Super-Agent 运维方案](../deploy/super-agent-v1/README.md)

## 理解设计

- [来源、路由、任务与执行职责](PROJECT_OVERVIEW.md#从消息到工作目标)
- [历史架构与权限契约](super-agent-v1/ARCHITECTURE.md)（标记 PLANNED） · [历史验收协议](super-agent-v1/ACCEPTANCE.md)
- [与 Harness 的关系](CURRENT_STATUS.md#与-harness-的关系) · [历史交接原件](harness-v4/SUPER_AGENT_HANDOFF.md)

## 核查证据

- [当前状态](CURRENT_STATUS.md) · [实验解读](EVALUATION_SUMMARY.md)
- [SA22 真实试用进展](super-agent-v1/SA22_PROGRESS_20261118.md) · [后续并行记录](super-agent-v1/SA22_PARALLEL_20261118.md) · [E/F 原实验](experiments/coordination-v2/README.md)
- [历史实施状态](super-agent-v1/IMPLEMENTATION_STATUS.md)（按记录日期阅读） · [SA00 固定接口机器记录](super-agent-v1/accept/SAV1-00/accept.json)

## 开发与历史档案

- [Super-Agent 历史任务书与专项总览](super-agent-v1/README.md) · [任务卡](super-agent-v1/TASKS.md) · [ADR](adr/)
- [V3.1 历史技术报告](TECHNICAL_REPORT.md) · [当时的 PDF](reports/20261106/MGAR-V3.1-Technical-Report.pdf) · [事实索引](reports/20261106/report-data.json)
- [V3.5 历史演示](v35/DEMO.md) · [P0 历史交付](repairs/P0-20261107/README.md) · [原实施档案](repairs/P0-20261106/README.md)

以下按阶段保留设计、历史验收和问题档案；较旧状态不能替代当前状态页。

## 运行与架构

| 阶段 | 设计 | 操作或验收 |
|---|---|---|
| R0/R1 基础与首个真实交付 | [边界ADR](adr/0001-v31-boundaries.md) | [R1真实验收](acceptance/r1-live-20261102.json) |
| R2 持续任务 | [Mailbox/安全点](adr/0002-r2-mailbox.md) | [工程验收](acceptance/r2-engineering-20261102.json) |
| R3 路由与任务关联 | [Router](adr/0003-r3-router.md) | [限定真实验收](acceptance/r3-20261102.json) |
| R4 跨源调查 | [知识/数据/专家](adr/0005-r4-investigation.md) | [运行](r4-runbook.md) · [真实对照](r4-live-report.md) |
| R5 受限修复 | [修复边界](adr/0007-r5-repair-boundaries.md) · [真实适配](adr/0008-r5-native-adapters.md) | [运行](r5-runbook.md) · [真实PR恢复](r5-live-report.md) |
| R6 监控/有限主动性 | [恢复请求](adr/0011-observe-only-watchdog.md) · [评测](adr/0012-r6-monitoring-proactivity-evaluation.md) | [运行](r6-runbook.md) · [故障矩阵](r6-failure-matrix.md) |
| 协调补验与修复 | [D/E/F设计](adr/0014-coordination-comparison.md) · [候选契约](adr/0015-coordination-candidate-contract.md) | [v1报告](experiments/coordination-v1/README.md) · [v2报告](experiments/coordination-v2/README.md) |
| 两项P0 | [冻结设计](adr/0016-p0-commerce-and-business-results-design.md) · [实施ADR0017](adr/0017-p0-commerce-implementation.md) · [复验ADR0018](adr/0018-exact-proposal-review.md) | [最终验收](repairs/P0-20261107/acceptance.json) · [原实验](repairs/P0-20261106/acceptance.json) · [运行](p0-commerce-runbook.md) |
| V3.2 通用仓库分析 | [运行手册](repo-analysis.md) | Web `/repo` · `mgar repo-analyze` · API `/api/v3/repository-analyses` |

## 结果与问题档案

| 档案 | 内容 |
|---|---|
| [R6完整修复](repairs/R6-20261103/README.md) | 46/90→74/90→87/90、数值/契约/权限/补丁协议修复、账单核对、原三项FAIL |
| [JSON模式请求事故](repairs/R6-20261103/json-mode-incident.md) | HTTP400、预检缺口、未知预占与严格账单核对 |
| [宿主与恢复](repairs/R6-HOST-20261105/README.md) | V2测试时钟维护、独立systemd VM、整虚拟节点/PG/角色/CAS恢复 |
| [E/F第二版修复](repairs/COORD-20261106/README.md) | 动作/UUID契约问题、级联失败、原子重核、恢复收据及Worker残余FAIL |
| [最终三臂逐项结果](../eval/reports/final-v6-results.json) | 30任务×3配置，87PASS/3FAIL |
| [最终三臂统计](../eval/reports/final-v6-summary.json) | 调用、费用、延迟、引用、置信区间和配对统计；消融NOT_RUN |

旧ADR和交付文件保留当时状态与限制；它们不会因后续修复被改写为“当时已通过”。最新结论应结合上述报告的时间和作用范围读取。
