# SA21 本地受控隐私、安全与恢复工程交付

**25 个新检查均有分版本 PASS；SA21 保持 IN_PROGRESS，等待独立验收。SA22 未启动。**

候选 `5be5dc736b7a1014d4ac75e823547b442522cb81`，官方源码 SHA256 `fdc012ae82b1ebaa1bcdccbfb56c4be95f7ac347c7aa7562dbe33d3dc5d1c8a1`。[机器摘要](accept/SAV1-21/accept.json)、[逐例记录](accept/SAV1-21/cases.json)、[证据清单](accept/SAV1-21/evidence-manifest.json)和[独立验收请求](accept/SAV1-21/INDEPENDENT_REVIEW_REQUEST.txt)给出命令、退出码、JUnit、归档、失败及清理事实。真实能力门仍为 NOT_RUN，固定 H12 真实 Agent/MVP FAIL 保留。

SA20 的 `7b4566d/9786256` 与独立报告、原探针和源码指纹已核对，并按用户授权关闭受控本地工程范围，见[关闭记录](accept/SAV1-20/independent-closure-20261115.json)。依赖 SA04、SA15、SA17、SA20 的已接受记录、完整任务书/卡片、验收、架构、数据保留与适用 ADR 的 UTC/SHA 均记录在本卡起始证据；原失败和各真实门保持原状。

本卡新增私有 `SourceSecurity(engine).inspect(cookie)`，检查当前 Owner 及项目，沿原 Engine 持锁连接只读统计最多 32 个来源的输入事实、tombstone、密文留存、待 purge 与失效密文。返回有界无正文元数据，超限为 partial，并明确表示检查本身不建立安全结论。不解密正文/凭据、不读 CAS、不另开业务写连接、不启动后台任务，也不提供付款、外发或恢复激活入口。清理继续调用原 Owner `SourceIngress.purge`。

没有修改共享 Engine/API、数据库 schema、预算算法或历史 SQL，没有新增迁移/接口、预占 0054 或导入 Harness/H12 活动实现；本轮不需要新的共享接线确认。增加的一个 JSON schema 仅约束本私有检查器输出。新的测试注册与关联清单哈希已经同步，完整 [保留策略与矩阵](SA21_SECURITY.md)说明每个阶段的最小审计和处置边界。

## 首次检查与定向修复

| 阶段 | 源码 | 检查范围 | 结果 |
| --- | --- | --- | --- |
| 首次正式 | `62f03f5` | `make verify TASK=SAV1-21`，25 项新组合检查 | 16 PASS / 9 FAIL；pytest 1、make 2 |
| 定向复验 | `5be5dc7` | `make verify TASK=SAV1-21-REPAIR-01`，仅原九项 | 9 PASS，0 FAIL/ERROR/SKIP；pytest/make 0 |
| 首次静态 | `62f03f5` | `make lint`、`make typecheck` | PASS；typecheck 328 个源码文件 |

首次九项失败来自本卡辅助夹具，原始日志和失败 JUnit 全部保留：策略读取使用了不存在的 `revision`，原字段为 `current_revision`；Docker 随机映射端口在容器重启后改变，探针仍连接旧地址；恢复测试重建 Gateway 时用了 1，而 SA13 原夹具已经将其推进到 2。修复使用原持久字段、重新查询独占容器实际端口及原 Gateway generation，没有改动产品模块或放松授权检查。

逐文件 AST 比较确认所有原断言不变，见 `fixture-repair01.json`。没有放宽阈值、增加重试次数或修改 PG 事实以伪造恢复。先前 16 项、历史 SA20 原13独立/55正式/88准备及已通过静态目标均未重跑。最新候选未单轮重跑全部25项；全部通过是按各自源码和原始证据绑定的结果。

## 实际覆盖的边界

- 四种真实来源操作（编辑、删除、撤身份、撤策略）提交后，原文、Memory、派生 CAS/导出路径与旧分页游标全部拒绝；PG 旧密文删除，最小输入事实保留。
- 三种失效分别发生在待发和已有 unknown 通知之后：待发取消，unknown 继续保留原账本；重复 tick 不发送，无查询能力时不猜测成功或重试。
- 假签名、跨会话和未绑定作者的注入内容不进入入站正文、数据库明文或检查输出。真实正文 TTL 到期后由原 purge 清理，输入与接收凭证继续保留。检查器必须使用当前 Owner，且不调用解密/CAS。
- 原模型端口经明确 MockTransport 注入流丢失/无效提案，只有一次受控请求；审计数据库不含正文，撤权后旧批次不再次发送，不生成任务/Action，调用账本保持真实已知或预占状态。这不是实际付费请求。
- 两个并发调度提交只形成一个 Assignment，取消后旧 Worker 拒绝开始工作；第二 Engine 无法取得原写锁；同库另一个合法项目的来源不进入当前项目检查输出。
- 两个独占 PG 服务实际 SIGKILL/重新启动：提交前无 ACK、未提交事实回滚；提交后旧输入持久，重放复用同一凭证和输入。实际 postmaster 时间变更、原连接失效、原始 PG WAL 恢复日志及重启后的端口均留存。主库没有用于故障注入。
- 实际旧 PG/CAS/密钥包在新空库/目录恢复，默认服务入口保持 hold；显式离线维护重放最新删除/撤权后，原文、Memory 和派生产物一起拒绝，最小事实保留。测试全过程未移除 hold 或激活服务。
- 在原 pg_restore 已完成后，独立子进程通过内核 RLIMIT_FSIZE 触发真实 EFBIG，原 CAS 写入失败；保留 `INCOMPLETE_RESTORE_HOLD`，恢复库中的原 Action 仍 unknown、Outbox attempts 仍为 1，默认入口拒绝启动。

源码/任务/配置/授权在每轮开始前提交并 Git 归档，归档重算官方源码指纹与工作副本一致；运行前后源码、HEAD 和输入均稳定。两轮归档、命令、结果、JUnit、观察和清理位于：

`evidence/super-agent-v1/SAV1-21/start-20261115T145313Z-d8e73443/`

其中 `initial25/` 保存首次原始失败及静态成绩，`repair09/` 保存仅原九项复验；两轮实际 pytest 日志/JUnit 的精确路径见机器摘要，未覆盖旧失败。`observations.json` 汇总 23 条无正文事实，`final-integrity.json` 保存最终盘点。

## 完整性与范围

正式仓库仍 50 份 SQL、19 份 Source 目录及654条引用，本卡另有13个私有产物引用，全部哈希一致。主库55435只读核对，仍44份迁移、无运维表；没有主库迁移或重启。两轮 PG 和恢复数据库/会话均清理，本卡故障容器、Assignment容器无残留，原H06及其他CLI资源保留。259个保护文件哈希一致，包含原验收摘要、用户 `.env` 和未跟踪文件、SA20独立报告/探针/失败证据及Windows副本。Harness活动树未读取或修改。

另按 SA20-I01 修复前保留的基线重核32,299份历史文件，原失败报告、探针、日志/JUnit、失败观测及Windows镜像均未变化，见 `historical-failures-preserved.json`；没有为了交付覆盖旧红灯。

这次本地工程结论限于注册的受控组合和明确绑定的历史证据。Source、模型、通知与数据都是合成 fixture；没有实际平台、付费、外发或发布。存储故障是内核 EFBIG，不是整盘 ENOSPC、物理掉电或云存储丢失。旧备份仍是可信离线操作者可读的历史副本，必须在hold中核对最新撤权/删除与外部账本，不能把包完整性当新授权；备份介质销毁、真实平台删除和长期运行未验收。

本地受控矩阵检查可以提交独立验收；这不建立所有平台、所有入口、所有故障下的全系统安全结论。完整真实安全、真实编码/渠道、发布与长期门继续 NOT_RUN，固定 H12 FAIL 保留，SA22 不自动启动。
