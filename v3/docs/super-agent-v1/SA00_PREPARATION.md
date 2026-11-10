# SA00 前置审计稿

状态：**DRAFT / 未冻结 / 未验收**。本稿是 Super-Agent 轨道准备工作；H01 尚未交付，
不把 SA00 标为 DONE，也不提前实施依赖未满足的后续运行时能力。

## 本轮已核对

### SA00-A01：Legacy trigger and identity

源码：`src/mgar_v3/domain.py:18`（`TriggerInput`）。

github/cli/web only; repository and positive issue_number required; repository/actor lowercased.

SA01/SA02 introduce versioned SourceEvent/ActorRef and trusted tenant/install identity; do not repurpose opaque Feishu IDs.

### SA00-A02：Business writer and ingestion

源码：`src/mgar_v3/coordination/engine.py:106`（`ingest`）。

Engine owns one advisory-locked PG connection; ingress is configured-project/repository/actor scoped; Thread key is repository#issue:n; source+external_event_id dedupe checks payload conflicts.

SA01/SA04 extend source keys, persistent ingress receipt, revisions/gap; keep the same Engine transaction authority.

### SA00-A03：Webhook source coverage

源码：`src/mgar_v3/api.py:1159`（`webhook`）。

HMAC-SHA256 verification and delivery ID required; workflow failures have a limited path; PR comments are explicitly ignored; no-PR workflow uses old issue-number fallback.

SA05 needs installation-scoped Issue/PR/CI/head-SHA links and separate source intake without fabricated Issue identifiers.

### SA00-A04：Router proposal contract

源码：`src/mgar_v3/routing/domain.py:34`（`RouterDecision`）。

Bounded proposals carry expected Thread revision, policy version, snapshot hash, expiry and optional epoch; old action set omits handback/unsubscribe.

SA09/SA11 require a formal versioned ADR extension of old R3 rules; do not delete deny branches as an implementation shortcut.

测试入口：`tests/test_routing.py:355`（`test_unknown_or_investigator_router_actions_fail_schema`）。本轮用于审计定位，不新增 PASS 声明。

### SA00-A05：Mailbox and subscription reuse

源码：`src/mgar_v3/coordination/engine.py:106`（`ingest`）。

Primary delivery and separate observer deliveries exist; mailbox events/queued jobs share a bounded count/byte budget and transactional rollback.

SA11/SA14 add TTL/cursor/control-role and safe-point input semantics without treating context as authority.

测试入口：`tests/test_duplex.py:246`（`test_mailbox_byte_backpressure_rolls_back_and_expiry_keeps_input`）。本轮用于审计定位，不新增 PASS 声明。

### SA00-A06：Legacy proactivity

源码：`src/mgar_v3/proactivity/store.py:236`（`_start`）。

Old policies start separate read-only speculative runs and mark actions empty; this does not provide general coding or a four-layer grant model.

SA03/SA07/SA16 implement separate receive/admission/execution/notification rules and shared project budget; Harness real-coding gates remain required.

### SA00-A07：External effects and reconciliation

源码：`src/mgar_v3/effects/dispatcher.py:22`（`Dispatcher`）。

Engine claims leased effects; timeout/transport ambiguity becomes unknown; reconciliation reads remotely outside the business transaction.

SA17 reuses this boundary for channels and notification grants; no unconditional platform exactly-once or blind retry.

测试入口：`tests/test_effects.py:58`（`test_timeout_after_remote_accept_never_reposts_and_reconciles`）。本轮用于审计定位，不新增 PASS 声明。

### SA00-A08：Watchdog

源码：`src/mgar_v3/recovery/watchdog.py:43`（`poll`）。

Limited loopback HTTP observer submits recovery requests; ambiguous submission is explicit and it does not import Engine or model clients.

SA20 expands source gap/lease/job metrics while preserving low authority and unknown-outcome protection.

测试入口：`tests/test_r6_watchdog.py:67`（`test_concurrent_observations_dedupe_without_progress_or_business_side_effects`）。本轮用于审计定位，不新增 PASS 声明。

## 待 H01 交付后冻结

1. 将 Trae 的正式接口/状态所有权映射与本稿对齐，评审具体提交后集成、复验。
2. 对来源身份、团队规则自主性、动态订阅和通知预授权形成正式 ADR；编号按当时最新
   ADR/迁移状态与 Harness 协调，避免双方并行使用相同编号。
3. 冻结首版接收范围、角色、数据外发与试用配置；没有平台安装和预算时保留禁发、
   不付费、不启动 Worker 的工程准备状态。
4. 冻结路由标注/独立持出协议、失败分母、共享预算和六类独立门。任务书建议的
   100 dev + 100 holdout、10 个真实正向任务及长期试用要求仍是待冻结/待执行目标。
5. 完成全部 SA00 产物与真实检查后再登记整卡验收，不用源码定位或文档检查代替运行能力。

## 分工

Codex 继续来源与控制平面的准备；Trae 继续 Harness H01。正式接口请求与集成规则见
`COORDINATION.md`。当前没有更改 Trae 工作树，没有新增机器人接入、通知或真实模型实验。

## 前置方案与复用验证

`sa00-draft/` 已形成首版范围、状态所有权、评测协议及 ADR 提案，全部标记 DRAFT/PROPOSED，等待 H01 接口后冻结。

本轮五项旧契约探针通过（0 失败/错误/跳过）：Router 非法动作拒绝、Mailbox 背压回滚、未知发送不重发与只读对账、Watchdog 并发去重、主动预算不产生新付费授权。使用本副本独立 PostgreSQL，运行前后源码指纹一致；不计为整张 SA00 或新能力验收。
