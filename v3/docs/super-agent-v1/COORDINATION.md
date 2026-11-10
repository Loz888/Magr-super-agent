# Super-Agent / Harness 协作约定

依据：用户于 2026-11-10 指定 Codex 负责 Super-Agent，本地 Trae 负责 Harness，
并允许按需调用 Trae 协作。

## 所有权

| 工作 | 负责人 | 工作目录 |
| --- | --- | --- |
| SAV1-00–SAV1-24：来源、身份、策略、Router、订阅、调度、Recall、通知与运维 | Codex | `/home/imini/magr-super-agent` |
| HV4-H00–HV4-H18：模型/工具/工作区/Broker/Verifier、编码循环、上下文与恢复 | Trae | `/home/imini/magr-harness` |

双方在每卡实施前完整阅读对应任务书与验收文件，并保存阅读时间与文件指纹。
源码与业务事实继续由原 Engine/PostgreSQL 协议约束；分工不会新增业务写者。

## 当前交接

Trae 仓库已提交 H00，并声明其工程/安全门通过；真实模型、Agent 和发布门未运行。
本副本已只读定位 H00 accept.json，尚未导入或复验该提交。H01 仍是 SA00 的未完成前置。
本副本分工前的局部 H00 静态清理保留，不再独立推进 Harness 整卡。

## 向 Harness 请求的 H01 交付

1. 已提交的稳定 commit、选型 ADR、接口 schema/版本、状态所有权映射和验收证据路径。
2. ModelAdapter、ToolRegistry、WorkspaceProvider、ExecutionBroker、ContextManager、
   Verifier、EventSink 的正式契约；哪些是冻结接口、哪些仍为建议需分别说明。
3. Source/策略生成的 Mandate 如何映射为 Task/Run，包含 actor/scope、repository/base SHA、
   workspace、input revision、execution epoch、预算预占/结算和 unknown 结果语义。
4. 持久 inbox/安全点、命令 Job 的启动/查询/取消、恢复与旧 Worker fencing 的能力边界。
5. Diff/tree、验证回执及 Review Bundle 的版本绑定；受控实现与真实能力门分别记录。

这些是 Super-Agent 的依赖需求，不预先指定或实现另一套 Harness。H01 未完成时只做
SA00 审计与需求准备，不宣称依赖满足或开放真实自动修复。

## 变更与集成

- 不直接修改另一方的活动工作树。交付用具体 commit、差异和证据，避免复制未提交文件。
- 共享 Engine/API/预算/领域对象变更先明确唯一实现负责人；迁移编号按集成时最新编号协调。
- 接口改变需说明版本、受影响 SA/HV4 卡、兼容方法及必要回归；不悄悄改变完成/授权语义。
- 在本副本审查与解决冲突，使用自己的数据库/Redis/临时目录执行验证后记录集成结果。
- 对话用于确认任务、接口和阻塞；稳定约定写回仓库。消息送达、对方确认、代码完成和
  本地集成复验是不同状态，分别记录。
- 外部写、付费模型、机器人接入与真实渠道实验仍按原任务书的具体授权及验收处理。


## 已接收依赖与下一步（2026-11-10）

H00 固定交付 de4775e 已集成。H01 v1.1 固定交付 38a65ba（实现 418d0c2）在本副本
merge 782f35c 后，独立原始权限探针 2 项、H01 注册 18 项、真实 PG 回归 5 项和
全库 lint/typecheck 通过。旧 e1b3214 两项权限 FAIL、原核心回归终态竞态 FAIL 均保留。
SAV1-00 冻结提交 6aeb1b0，其注册 120 项通过，正式验收见 accept/SAV1-00/accept.json。

已向 Trae 当前对话发送接收结论并要求按任务书继续 H02；首次消息的接收已由交接
文档确认，本次接收结论的后续确认以 Trae 新交接记录为准。消息待发送不视为已送达；
若需在任务执行期间立即纠正，使用待发送消息中的“直接发送”并确认处理结果。
Sources 0030 已在 H01 交接确认归 Codex，尚未创建；双方 ADR 使用轨道内命名空间。
共享 Engine/store 的终态原子性修复已明确协调由 Trae 负责并纳入此固定交付，
Super-Agent 不同时编辑这些文件。下一卡 SAV1-01 仍须完整重读任务书及对应要求后实施。


## SAV1-01 完成与交接

实施提交 8dc641bbdf1fe7d0f057e11ec66e951f2874f2f8，验收提交 5fda544。
source-contract-v1/source-event-v1 与 0030_source_contracts.sql 已落地，注册验收
79 项通过；源码指纹绑定 schema 和合成样本。接口与能力边界见 SA01_CONTRACTS.md，
给 Harness 的完整交接见 HARNESS_SA01_HANDOFF.md。新来源身份不直接转成 Harness actor。

Trae 正在执行 H02 修复；非紧急 SA01 交接已排队，UI 明示“等待发送”，当前未收到
Trae 确认。没有使用“直接发送”打断其检查。来源/执行/平台监听仍禁用，H02 的活动
文件未由本轨道编辑。下一卡 SAV1-02 的共享迁移编号需先另行对齐。


## SAV1-02 实际确认与固定交付

SA01 交接已由 Trae 固定提交 6109ce5 §12 确认；上文等待发送为当时状态，历史保留。SA02 的 0031 迁移与新身份管理 API 路径已于 2026-11-10T14:50:28Z 获 Trae 实际确认，固定提交 0ca76063ab91177cd7255c475435d3d6b00c726c §13，凭据见 SA02 起始阅读证据。

本卡实施 3bcfb0234789e000ed26597bc8a6af378675791d，注册 132 项 PASS，具体说明 HARNESS_SA02_HANDOFF.md。没有编辑/导入 Harness 活动文件，也没有将 H02 变成已验收。新能力未启用真实平台/模型/外发。SA03 仍 PLANNED，0032 未预占。此轮完成交接消息尚未发送/确认，不能把文档准备当送达。


## SAV1-03 实际所有权确认与交付

SA02 已由 Trae 在固定 4ad86c1 §15 实际收讫；上文 SA02 完成消息未发送为历史状态，保留。0032/source-policies API/受限 Engine 接线实际确认 4ad86c1 §15，ToolAuthority._grant pre-charge 接线实际确认 f61a233 §16；两份确认与 UTC 记录在 SA03 起始证据。

SA03 实施 9f64dc9000879ae84a71640a6d96bdb4668b332e，注册 304 项通过，见 HARNESS_SA03_HANDOFF.md 与验收摘要。没有修改/导入 Harness 活动文件，不改变其 H03/H04 状态。SA03 完成消息未发送/未确认，文件准备不能当送达。等待独立复验，不自动进入 SA04，0033 未预占；真实来源、模型、外发/发布保持未启用。
