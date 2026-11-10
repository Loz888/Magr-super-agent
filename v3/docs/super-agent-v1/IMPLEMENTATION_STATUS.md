# Super-Agent 本机实施进度

## 当前分工（2026-11-10 更新）

Codex 负责 SAV1-00–SAV1-24；Trae 在 `/home/imini/magr-harness` 负责 HV4-H00–HV4-H18。
当前转入 **SA00 前置准备**，卡状态仍 PLANNED；Harness H01 尚未交付。
H00 已在 Harness 仓库声明完成，但尚未集成到本副本，不沿用本机局部 H00 的整卡状态。
分工、接口请求和集成规则见 `COORDINATION.md`，机器可读状态见 `coordination-state.json`。
本副本不再独立继续 Harness 卡；下面保留分工前初始化工作的历史记录与证据。

---

## 分工前的初始化记录（历史快照）


更新：2026-11-10。工作副本：`/home/imini/magr-super-agent`，分支 `super-agent-v1`。

## 当前状态

当前卡 **HV4-H00：IN_PROGRESS**。HV4-H01 与 SA00 尚未开始。上游 TASKS.md/plan.json
保留原始规划快照；本文件与 Harness `baseline.json` 记录这份工作副本的实际实施进度。

- 已从干净 V3.5 应用基线建立独立 Git 副本，源码与历史保留，未复制原运行凭据或数据。
- 已加入 Harness V4 与 Super-Agent V1 的 14 份规划文件及来源指纹。
- 已在根 AGENTS.md 固定用户规则：每卡实施前完整阅读对应任务书、引用要求、验收与 ADR，
  并记录读取时间/指纹；不能仅依赖旧摘要。H00 阅读凭证已归档。
- 已创建独立 Python 3.12 锁定环境及 PostgreSQL/Redis，完成这份副本的全部迁移。
- 已修复初始 23 项 lint、16 项类型问题；全库 lint/typecheck 通过，检查范围未缩小。
- GitHub 分支列表的错误响应现在返回受控 DomainError；新增 5 个输入边界回归。
- 首页相关 61 项、V35-R05 27 项、针对性 29 项全部通过；各组有重叠，不相加为独立总数。
- 工具 catalogue 与原基线逐字一致；旧 V3.5 FAIL/NOT_RUN 和验收文件未改。

## H00 剩余工作

完成能力审计和任务类型契约；在至少四个真实项目上冻结 12 个开发任务与 12 个独立
持出任务、资源上限和判定器指纹；注册真实 H00 验收并通过后，才进入 H01 与 SA00。
H00 整卡门禁尚未放行；这里的局部检查不代表真实通用编码、渠道接入或长期运行通过。

## 证据

原始日志位于 `v3/evidence/harness-v4/HV4-H00/`；注册验收日志另外位于
`v3/evidence/AGENT-UI/` 和 `v3/evidence/V35-R05/`。失败尝试全部保留。
首次命令因非交互 PATH 缺少 uv 失败，后以正确工具路径重跑。
针对性测试通过时出现旧共享 pytest 临时目录清理警告；后续根 Makefile 已设置本项目
独立 TMPDIR，未删除其他项目的临时文件。
