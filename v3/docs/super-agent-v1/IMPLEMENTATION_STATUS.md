# SAV1-05 独立复验修复工程通过，等待再次独立核验

固定修复 cc1756bfc333ba9204761a6dde6875f0124bcd0b，源码 14bcac359c25ff1469aac9ee3d39ae51adfb5eb651b24be8602435c51a5ada71；完整注册543 PASS（原SA05 79 + 修复44 + 既有420），0 failure/error/skip；原独立10项原样重跑PASS，lint/typecheck 222 files PASS。完整注册结果 evidence/SAV1-05/20261111T043250Z-2a80700f/result.json 由原评分器真实生成，前后指纹一致。详见 SA05_REPAIR_20261111.md 及 accept/SAV1-05/repair-20261111.json。

两类缺陷修复和原失败记录分开保存；原独立499项虽JUnit/日志全绿但汇总器Hangup、无result.json的事实未改。旧测试/0001–0034/schema/评分器保持原样。工程PASS不替代新的独立验收，SA05仍IN_PROGRESS、SA06仍PLANNED。无Harness操作，无真实API/模型/外发/发布；完整系统安全及全部live/长期门NOT_RUN。以下为历史记录。

# SAV1-05 独立复验失败修复中

独立 HEAD 4ecac50 的新增10项探针为4 PASS/6 FAIL；原499项 JUnit 虽全绿，外层汇总器因 Hangup 无 result.json，不补造结果。原交付摘要另存 accept/SAV1-05/history/4ecac50-accept.json；下方旧“工程通过”均为历史快照。

两类缺陷已修复：受信编辑/删除的大正文不阻断旧内容失效；GitHub 当前操作者与资源原作者分开，moderator-first 后原作者变更继续执行可信失效。新增44项正式真实PG回归、原独立10项原样重跑及 lint/typecheck 222 files 已通过；完整固定提交注册验收尚待执行。SA05 保持 IN_PROGRESS，SA06 PLANNED。无 Engine/API/schema/迁移/预算/ToolAuthority/Harness 变更；真实平台/模型/外发/发布/完整安全与长期门 NOT_RUN。

# SAV1-05 工程通过，等待独立验收

固定源码 fce886a8af571b86b7d0502b58874ee3c2ceeba4；完整注册499 PASS（SA05 79 + 既有420），0 failure/error/skip；make lint、make typecheck（222 files）PASS。已应用本副本0034，原0001–0033和Engine/ToolAuthority/财务代码未改；旧Webhook业务分支AST未变，补充同一Engine锁内的owner裁决与旧workflow claim防重。详见SA05_GITHUB.md及accept/SAV1-05/accept.json。

保留初轮gap用例列名失败、静态失败、两项灰度切换失败及两次主动中断的完整注册记录；当前通过结果不覆盖这些原始记录。受控GitHub/HMAC/RS256/HTTP与真实隔离PG只构成工程证据，真实平台/API、模型、完整系统安全/编码/发布/长期门仍NOT_RUN；本轮新增付费/外部业务写为0。SA05保持IN_PROGRESS，停在本卡等独立复验，SA06未启动。下方旧条目均为历史快照。

# SAV1-05 已实施，完整注册验收中

用户已确认 SA04 独立复验通过，现进入 SA05；下面旧的“尚未进入 SA05”均为历史快照。SA05 新增77项真实PG/受控GitHub检查通过，lint/typecheck 222 files通过；完整注册验收尚待固定源码后执行，不提前记整卡PASS。本副本已应用0034；真实平台/API、模型、外发/发布、完整系统安全与长期门NOT_RUN。完成工程后停在SA05等待独立验收，不进入SA06。

# SAV1-04 排序转换修复工程通过，等待独立核验

源码 b15937d6dd1ecacdf720199fcd14534c05ffd986；420 注册 PASS（SA04 116、排序新增 25），原独立探针 7 PASS，lint/typecheck 215 files PASS。SA04 保持 IN_PROGRESS，不进入 SA05。本轮只处理 Super-Agent。见 SA04_ORDER_TRANSITION_20261111.md 与 accept/SAV1-04/order-transition-20261111.json。

# SAV1-04 排序转换修复验证中

新独立差分修复前 1 PASS/1 FAIL 已原样保留；统一接收/拒收排序分类后 25 项新回归、旧修复与原独立 28 项、lint/typecheck 215 files 通过。完整注册尚待执行，SA04 保持 IN_PROGRESS，不进入 SA05。本轮仅 Super-Agent，不跟进 H 系列。

# SAV1-04 修复工程通过，等待独立复验

原独立复验 P1/P2 已修复；原评分器原样复跑 5 PASS，完整注册 395 PASS（SA04 91），lint/typecheck 215 files PASS。修复提交 84ca5c3c688b825dc32763a834cc7cc973b0cb69。SA04 保持 IN_PROGRESS，不进入 SA05。H04 仅材料核对，安全门未放行；详见 SA04_REPAIR_20261111.md 和单独 docs/material-reviews/H04-20261111-materials.md。

# SAV1-04 当前交付

SAV1-04 DONE（本卡工程，等待独立复验）。实施 ad4aae84bdc61750626eeaefc17eabf9d28c4a65；注册 374 PASS，其中 SA04 70，0 failure/error/skip；lint/typecheck 215 files PASS。见 SA04_INGRESS.md 和 accept/SAV1-04/accept.json。真实平台/SDK、模型、完整安全/编码/发布/长期门 NOT_RUN。无付费或外发，停在 SA04，不自动启动 SA05，0034 未预占。

# SAV1-03 当前交付

SAV1-03 DONE（本卡工程验收，等待独立复验），source-policy-v1 已固定。实施提交 9f64dc9000879ae84a71640a6d96bdb4668b332e；注册 304 项 PASS，其中本卡 88 项，0 failure/error/skip；全库 lint/mypy 208 files 通过。见 SA03_POLICIES.md 与 accept/SAV1-03/accept.json。真实平台/编码/发布及完整安全/长期门仍 NOT_RUN，无付费或外发；停止在 SA03，不自动进入 SA04，0033 未预占。

# SAV1-02 当前交付

SAV1-02 DONE（本卡工程验收），source-identity-v1 已固定；实施提交 3bcfb0234789e000ed26597bc8a6af378675791d。注册检查 132 项通过，其中 SA02 身份用例 52 项；lint/mypy 204 files 通过。见 SA02_IDENTITIES.md 和 accept/SAV1-02/accept.json。真实平台、模型、外发/发布和长期运行仍 NOT_RUN。SAV1-03 PLANNED，待独立复验后按完整阅读与共享所有权协议启动。

# SAV1-01 当前交付

SAV1-01 DONE，source-contract-v1 已冻结。注册验收 79 项、定向 51 项通过（有重叠），全库 lint/typecheck 通过；实施提交 8dc641bbdf1fe7d0f057e11ec66e951f2874f2f8。0030 已在独立副本应用。说明见 SA01_CONTRACTS.md，正式摘要见 accept/SAV1-01/accept.json。下一卡 SAV1-02 PLANNED，实施前须完整重读任务书；真实平台、策略和自主执行仍按各卡验收。

# 当前实施状态

SAV1-00 DONE：范围、正式 ADR、状态权威、复用审计和试验协议已冻结，注册回归 120 项通过（无失败/错误/跳过）。H00 与修正后的 H01 固定提交已集成并独立复验。正式交付见 `sa00/README.md`；旧准备稿保留为历史，不作为当前状态。

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
