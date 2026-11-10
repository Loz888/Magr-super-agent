# SAV1-00 冻结包

本卡交付范围、复用审计、正式 ADR、状态所有权、需求映射与试验协议；新增运行能力在后续卡实施。整卡结果以 `../accept/SAV1-00/accept.json` 为准。

- `baseline.json`：V3.5/H00/H01 固定提交、独立复验与历史验收哈希。
- `scope.json`：单组织/管理员/项目/仓库/机器人/群、四角色、事件、数据保留与外发边界。默认 Shadow、禁发、零预算；真实安装和试验 manifest 尚未配置。
- `reuse-audit.json`：八个现有调用链的源码符号、哈希、实际限制与真实回归入口。新来源/自主模式不继承旧许可。
- `../adr/SAV1-00-authority-sources-policy.md`：对旧 R3/只读主动性做版本化扩展，Engine 继续唯一业务写者；通知预授权和精确发布分开。
- `state-ownership.json`：Engine/PG/CAS/LangGraph/Redis/Source/Harness 的权威与真实事件 schema 映射，0030 迁移分工。
- `requirements-map.json`：E01–E22 到实施卡、Harness 前置的完整映射。
- `evaluation-protocol.json`：200 条路由集、独立标注/holdout、失败分母、共享预算、10 个真实正向任务与 72h/72h/5 工作日协议、SLO 与六个独立门。阈值冻结，数据集和真实试用尚未执行。

复验：在独立工作副本根目录运行 `make verify TASK=SAV1-00`。它执行实际旧协议、Engine 单写者/事务、验签/幂等、Router/Mailbox、未知副作用、主动策略/Watchdog 与 H01 契约的回归，不是空测试或只检查文档字符串。检查通过只证明本规划卡范围，系统来源/编码/发布/长期运行门保持 NOT_RUN。

具体平台 ID、原文外发、付费额度、渠道通知与发布均需后续受信配置/许可。本包不是安装或线上激活记录。历史失败及旧候选不改写；`../sa00-draft/` 为冻结前历史草案。
