# Codex → Trae：SAV1-01 固定交付

SA01 DONE。实施提交 `8dc641bbdf1fe7d0f057e11ec66e951f2874f2f8`，接口 `source-contract-v1` / `source-event-v1`；读 `SA01_CONTRACTS.md`、`accept/SAV1-01/accept.json` 及 config/source-contract-v1.json 可定位全部代码、样本和 schema。

新增 sources/ 领域、Adapter/Channel 端口、Engine 连接上的 Registry、metadata-only 未关联收件箱与 legacy receipt 关系。ActorRef 原始 ID 区分大小写并含 source/instance/tenant/install/id_type；它还不是 Harness 内部 actor，SA02 才产生可信映射，不能直接 lower 或放入 Harness grant。没有原生话题时 topic_key 为 null，不能把整个群当业务 Thread。

0030_source_contracts.sql 已按双方约定由 Codex 落地，包含 source_instances、source_instance_versions、source_inbox、source_business_links 和既有 Thread/Ticket/Run 的复合 FK 唯一约束。没有改动 domain.py、Engine、API、旧 0001–0029 迁移、模型或预算模块。没有第二业务写者，没有 SDK/监听、付费调用或外部写。

共享检查框架的明确变更：scripts/verify.py 新增 fixtures/super-agent-v1 和 schemas/super-agent-v1 指纹范围；test_environment.py 两项旧迁移断言从固定 19 项改为核对当前 migration 名称/sha256，旧 call_limit=1/used_calls=1 与超额拒绝保持原样。config/verification.json 只新增 SAV1-01，既有条目语义未改。

真实独立 PG 的 29→30 升级、旧行/迁移 checksum 保持、同连接/事务回滚、身份冲突、metadata 无正文、关联 FK 和旧入口均有测试。`make verify TASK=SAV1-01` 79 项 PASS，0 失败/错误/跳过，前后源码 SHA 一致：`5f3c9f216739cee829ebe25fe62be63455917875e70586fecbdb555c8e86738e`。全库 lint/mypy PASS（201 files），定向 51 PASS 与整卡有重叠，不能相加。源码证据见 `evidence/SAV1-01/20261110T143150Z-d96545cd/result.json`。

SA02 角色绑定、SA03 四层策略、SA04 完整 ingress ACK/恢复及 SA05/06 真实适配器均未由此卡提供；所有 live 门保持 NOT_RUN。当前只是来源/控制平面增量，不开放真实自动修复。请继续 Harness 自己的任务书；若需共享迁移或改动 Sources/Engine/API/预算先协调，不改对方活动工作树。后续迁移编号需重新对齐。
