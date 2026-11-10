# ADR SAV1-03：分层策略与不可逆版本 fencing

状态：ACCEPTED FOR IMPLEMENTATION，source-policy-v1。继承 SAV1-00/02 与旧 ADR0001/0003/0007/0012 的边界。

Receive、Admission、Execution、Notification 分别有显式字段，来源身份不由正文/模型产生。disabled 保留独立 Receive，shadow 包括显式请求，auto_investigate 禁写，auto_repair 的真实执行仍需 Harness/Worker 门。旧只读、显式请求、支付和发布许可不升级。

控制台当前主人以 cookie/Origin/CSRF 保存明确内容 hash、UTC、review_ref 与版本；保存/撤销 CAS，历史不可变。签署是会话授权审计，不声称公钥数字签名。预览完全只读，候选事实和决定不成为执行凭证。

0032 只增加授权/签署/审计事实与原 Run/Action 关系，Engine 同一持锁 PG 连接继续是唯一业务写者。修订、撤权、来源/身份变化立即失效关联授权、推进原 epoch、到期原租约、取消未派发工具与消息；在途结果/unknown/财务账本保留真实结算/对账，不恢复任务或盲重试。过期时逐操作拒绝，显式维护可持久清扫；真实进程停止与常驻任务另验。

运行关联只限制已有 fixture 调查，不创建或扩大任务；真实桥在 SA04/SA13。逐工具检查放在旧 ToolAuthority charge 之前；无关联旧 Run 立即返回。来源通知必须绑定精确旧 Action/body/目的地，派发前重查 quiet 与频率，仍需旧精确审批，不授予 Draft PR 权。预算定义不创造付款，SA07 前 model_call fail-closed。

共享所有权实际确认见 Harness 固定 4ad86c1 §15 与 f61a233 §16；不修改 Harness、模型或费用算法。全部平台/付费/外发/发布/长期运行 NOT_RUN，无默认激活。详细接口、回滚、在途处理和限制见 ../SA03_POLICIES.md。
