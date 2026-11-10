# ADR SAV1-02：平台身份与当前控制权

状态：ACCEPTED FOR IMPLEMENTATION，契约 source-identity-v1。

## 决定

平台身份只能由服务端受信 verifier 根据签名/安装证明产生。正文、显示名、引用、模型结果、legacy verification_ref 和公开 JSON 断言均不授予身份。保留 SA01 完整安装/租户和大小写敏感 opaque ID，verified kind 首次确定后不可变。

复用唯一控制台主人、当前服务端 cookie、Origin/CSRF。跨平台及主人关联只由管理员显式核对已有 receipt 并选择 principal，不自动合并同名用户。来源 principal 是新的内部 ASCII actor，不修改旧 Engine/Harness allowed_actors 或 grant。角色独立、绑定于会话，控制任务额外要求精确 Ticket membership；机器人/服务仅提供上下文。

签名验证在事务外，管理与授权在原 Engine 的同一 PG 连接内完成。随机内部 bearer 只存 hash，并绑定来源/身份 revision 及短期时限；角色/控制者变更通过 PG trigger 使旧 bearer 失效。授权检查与受保护的业务变更共享事务，撤权提交后禁止新操作；外部调用仍必须在事务外并遵守后续 policy/epoch 检查。

身份映射、角色、receipt 与最小审计是授权事实，不新增第二个 Thread/Ticket/Run 状态机。0031 及新身份管理 API 的所有权已获 Trae 固定提交 0ca76063ab91177cd7255c475435d3d6b00c726c §13 实际确认。

## 后果

来源角色不构成执行扩权或发布审批。身份/绑定撤销不能由新消息自动恢复，恢复需主人显式操作及撤权后新 proof。绑定到 principal 后首版不提供静默换绑。成功审计随业务事务提交；外层事务回滚也会回滚嵌套审计，不另开写者。

服务默认无生产 verifier；真实 GitHub/飞书身份在 SA05/06 单独验收，完整入站和四层策略在 SA03/04。工程 HMAC 合成重放与 PG 回归通过不意味着真实平台、编码、付费或外发能力放行。详细角色矩阵、API、恢复和限制见 ../SA02_IDENTITIES.md。
