# Codex → Trae：SAV1-02 固定交付

实施提交 `3bcfb0234789e000ed26597bc8a6af378675791d`；契约 `source-identity-v1`；注册检查 132 项 PASS（SA02 52 项，0 failure/error/skip），全库 lint/mypy 204 files PASS。原始结果 `evidence/SAV1-02/20261110T154000Z-d6fa7dce/result.json`，前后源码 SHA `0be627fdeaf1e731d4d9f023bcbeceb72279a9a173b1a37415bc475373c9beb5` 一致。验收和限制见 accept/SAV1-02/accept.json、SA02_IDENTITIES.md。

双方固定确认依据为 Harness 提交 0ca76063ab91177cd7255c475435d3d6b00c726c §13（2026-11-10T14:50:28Z）：0031_source_identities.sql、新 /api/v3/source-identities/ 管理路径及 api.py 最小接线由 Codex 实施。当前已完成；没有修改 Engine、模型、预算、Harness、旧迁移 0001–0030 或旧 API 行为，仅新增 import/register 两行。未导入 Harness 活动工作，H02 验收状态不受此交接改变。

来源 ID 保持大小写与完整 scope。平台证据经服务器 verifier 校验，管理员显式关联内部 principal；engine_actor=sa_<uuidhex>。内部 actor 仅是身份映射，不是旧 allowed_actors 或 Harness grant，角色也不构成 ExecutionGrant。上下文、显示名、引用、模型输出与普通 IM“同意”均不能扩权/审批。

SourceIdentities 使用现有 Engine 持锁连接。受保护的变更必须位于 authorized(...) context 的同一事务内；不得缓存 IdentityAuthorization。角色/撤权/控制者变更推进身份 revision；旧 proof 立即失效。任务控制必须有当前会话 role 及精确 Ticket membership。机器人/服务仅提供上下文。未来 SA03/04/13 接线仍须即时检查 policy/epoch/预算，禁止把此身份映射直接当工具/发布授权。

默认生产 verifier 为空，没有真实平台监听、付费模型、Worker、通知或发布；所有 live 门 NOT_RUN。工程签名负例用真实 HMAC 校验合成信封，不声称 GitHub/飞书 SDK 已验收。下一卡 SA03 及共享迁移 0032 尚未实施/预占，涉及共享 Engine/API/预算前需实际协调确认。

本文件是具体可审查交付。准备文件不等于消息已发送/已读或 peer 复验通过；协作状态另记 coordination-state.json。
