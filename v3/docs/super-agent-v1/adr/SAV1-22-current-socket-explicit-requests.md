# ADR SAV1-22：未补拉缺口与新连接中的显式请求

状态：受限真实试验修复，2026-11-17；继承 SAV1-04/06。

首次真实 L01/L02 的两条输入相隔 3.077 秒到达，但未创建 Coding Task。
原生富文本在中文片段之间插入空格，使逐题匹配失败；来源先前存在未补拉的断线
缺口，又使新连接收到的两条请求永久成为 context_only。旧记录和已发送回复保留，
不得将它们升级或重放为编码授权。第二条还被问答模型判成 answer，未派发编码。

未支持历史补拉的区间继续保留 gap，不写 resolved_at、不声称补齐。仅当前私有
SocketProof 核验成功、Feishu installation_proof、verified_source、人类当前 requester、
结构化 mention、create 的新鲜输入，可凭本条新请求进入原 Admission/Execution 检查。
历史、过期、缓冲重交、仅上下文角色、Shadow 及普通 Webhook 仍受原 context_only
规则限制；缺口不会转为团队自主规则的授权。原权限、源 revision、策略、预算、
Mandate 和 Coding 检查均保留，不修改旧输入。

固定题目匹配仅忽略中文字符间的平台空白；ASCII、路径及参数边界不合并。
Owner 已冻结且完整匹配的明确编码命令，由受信注册器决定派发；模型意图作为建议
另存原 Run event，不可静默降为实现建议。未登记命令沿用现有路由。

不新增 Engine/API/schema/财务接口，不延长编码期限、不增加预算、不导入 Harness。
新边界用真实隔离 PG 和受控传输首次检查；只有新群消息才能复核真实入口，之前
两条失败及实际费用仍计入分母，工程通过不等于真实修复或整卡通过。
