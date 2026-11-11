# ADR SAV1-04：单写者持久接收与保守恢复

状态：ACCEPTED FOR IMPLEMENTATION；source-ingress-v1，继承 SAV1-00/02/03。

受信平台 verifier 在事务外验签，唯一 Engine 在同一持锁 PG 连接中重查当前 Receive/身份/owner generation 并提交 receipt/inbox/version/cursor 后才允许 ACK。拒绝外层 savepoint 冒充 durable commit。生产 verifier/SDK 为空，早 ACK 模式不启动；真实平台可靠性单独验收。

传输、事件、实体、编辑版本分离去重；正文冲突显式保留，未知顺序保守失效。已删除/撤权/过时输入不能被重复事件、新授权或重放复活。缺口只有实际回执覆盖才标恢复，历史一律 context_only；没有历史能力保留 gap。新来源输入归属原 source_inbox，不复制业务主状态机或伪造 Issue。

Receive 外正文不持久化。许可正文采用 scope-aware 读取、独立加密、TTL 与事务撤权清理；最小 hash/tombstone 和正文生命周期分开。可选短期、私有、加密限额缓冲只重交 Engine，不 ACK、不裁决业务。PG 不可用且没有新鲜预鉴权时不缓存正文。

0033/API/Engine 最小接线已获 Trae 固定 a04dbde §17、bd50830 §19 实际确认；唯一旧新 owner 与 CAS 防止双入站。其余 Engine 方法、ToolAuthority、财务算法、旧迁移与 Harness 不改。运维/备份恢复及真实 SDK、完整安全/编码/发布/长期门保持后续验收；详细协议、命令和限制见 ../SA04_INGRESS.md。


2026-11-11 独立复验修正：接收/留存决定不能抹去已可信观测的新编辑水位。复用既有单调 native_order 和 sticky ambiguous 保存无正文事实，head 仍只引用 canonical input；等水位而无 canonical 证明也不复活。新版本隐私失效前先做等价事件/版本去重，避免无序同版本重投删除自身正文。无 schema/迁移/Engine/API/财务或 Harness 变更；原 FAIL 与评分器保留，修复后重新注册验收并等待独立复验。

拒收的带版本 create/历史快照也遵守同一观测水位与无序歧义规则；不能依赖 operation 名称忽略已知新版本。新增回归覆盖该形式及晚到 create 不得清除较新 canonical 正文。中间候选 3eca833 的 390 项 PASS 和快照邻接 1 FAIL 均保留，最终结果另行验证。
