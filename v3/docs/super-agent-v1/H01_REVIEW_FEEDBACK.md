# H01 对接评审反馈

状态：待 Harness 所有者确认。基于 H01 活动交接稿的只读评审，尚未导入未提交代码。

## 1. 事件游标与步骤字段应映射到真实 schema

HARNESS_CONTRACTS.md 与 STATE_OWNERSHIP 当前写 `app.run_events.seq` 和
`run_events.operation_id`。现有 `migrations/0001_coordination.sql:156` 的真实列是
`event_id bigserial`、`run_id`、`kind`、`safe_payload`、`created_at`；没有 seq 或
operation_id 列。`Engine.events()` 按 `event_id > after` 排序读取。

建议 H01 明确：EventEnvelope.cursor 投影到现有 event_id；operation_id 及信封 UUID
属于 safe_payload 的版本化字段，或者说明具体持久适配将在 H09 前向迁移中交付。
本轮不要因文档映射错误直接创建新的业务状态表或修改现有迁移。

## 2. SA00 与真实自动修复的前置应分开

SUPER_AGENT_HANDOFF.md §8.4 将 SA00 限制到 H02–H07/H11 完成之前都不能满足依赖。
原 SAV1-00 的前置是 H00/H01；它是范围、复用审计、ADR 与验收协议冻结卡。
请明确 SA00 可在 H00/H01 交付复验后完成这项冻结，真正自动修复仍等待 H11/H12
及所需工作区/Broker/编码能力验收。不能扩大 SA00 的 DAG，也不能提前开放修复。

## 3. actor 应明确是受信内部主体，不能直接接收平台 opaque ID

TaskBinding.actor 使用 legacy ASCII 正则并 lower()；飞书 ActorRef 的原始 ID 可能区分
大小写且需安装/租户范围。建议说明此 actor 是经可信 SA02 映射的 Engine principal，
原始 SourceActorRef 保留原样、单独存储。禁止正文/模型推断映射或对原 ID 直接 lower()。
如果 v1 预期直接承载原始平台 ID，则需重新讨论字段/校验契约与版本。

## 4. 只读许可与未声明工具的默认拒绝缺口（需修复）

对已提交 `e1b3214af8ae62f76e99e322204f05e055133490` 的隔离源码快照，执行了两个
仅增加内存计数、不读写文件/网络/数据库的受控探针：

- Registry 声明 `custom_write` 为 write，而 CapabilityGrant.write_paths 为空：处理器仍被调用一次。
- Model 返回 Registry 从未声明的 `custom_write`：处理器同样被调用一次。

根因：`HarnessRuntime._precheck()` 只校验四个固定工具名，对其他名字返回 None；
`_dispatch_tools()` 随后调用可替换 Registry.invoke。因而“任何适配器不能绕过权限入口”
在 H01 参考回路上还不成立。请在 Harness 所有者一侧修复并加入真正的负向契约回归。
H01 可先默认拒绝未支持工具；若要支持通用扩展，必须有明确的能力校验入口，不能只信任工具名。

证据：`evidence/super-agent-v1/SAV1-00/20261110T131512Z-h01-review/result.json`，
完整可复现脚本位于同目录 `probe.py`。这是 H01 参考实现的契约问题，不是生产事故；
未执行实际写入、付费调用或修改 Harness 工作树。修复复验前，本副本不把 H01 作为已接收依赖。
