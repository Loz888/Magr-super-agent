# SA22 飞书固定回复接通记录

2026-11-16，按用户“继续接回复后端”的指令，Bot 群内 MAGR助手已实际回复三条不同的
本人 @ 消息。三个原 Action 均为 succeeded、平台回执 accepted，原 Outbox 各派发一次；
前两条已在飞书桌面核对显示，第三条为后续只读回执观察。当前支持固定收信／状态说明，
尚未启用模型语义问答或自动编码。
SA22 保持 IN_PROGRESS；本记录不是完整 SA22 或独立验收通过结论。

机器记录见 [reply-backend-20261116.json](accept/SAV1-22/reply-backend-20261116.json)，
协议说明保留在 [SA22_REPLIES.md](SA22_REPLIES.md)。

## 实现与授权

新 `source_replies` 只处理当前获准来源、本人可信身份、结构化 @ 和当前通知授权，
以 `source-receipt-reply-v1` 标记原 Thread/Mandate/Ticket/Run。复用原
Action/Approval/Outbox/Dispatcher；唯一原输入并发或重投复用同一回复。
这些 Run 是 waiting_input 的接收回执工作流，不能计作编码任务成功。

新 `FeishuNotificationChannel` 使用真实 reply API 回复原消息，凭据从原加密 Vault
解析。HTTP 在原 Engine 事务外；取得令牌后重新检查当前来源、策略、身份、凭据与
目的地。明确拒绝记失败，响应丢失／不完整响应保留 unknown，不盲重发；
平台幂等能力声明 unknown，回执查询声明 unsupported。

本轮为 Bot 群、本人、固定 verified_result/public_summary 单独保存通知授权。
Source revision=4、Feishu config revision=2、Ingress generation=2、policy revision=2。
通知最多20条／小时，冷却3秒，内容最多512字节；正文固定为：

> 已收到你的 @ 消息，消息接收与回复服务已连通。当前为受限试用，尚未开启自动编码。

模式为 auto_investigate，仅用于这份独立通知许可；Admission/Execution 关闭，
model_input=false。原输入正文加密，可读TTL为300秒，每30秒清理过期正文，不回显原文。
没有新增迁移、共享 Engine/API/schema/预算算法变更，也没有修改或导入 Harness 活动树。
没有占用0054。

## 分版本检查

| 范围 | 固定版本 | 结果 |
| --- | --- | --- |
| 新登记回复检查 | d46f5d7758d28d0690aaa1e3a547a6785c738536 | 11 PASS，exit 0，真实独立 PG＋受控 HTTP |
| typecheck | 同上 | PASS，exit 0，330个源码文件 |
| 首轮 lint | 同上 | FAIL，exit 2；原始15项格式问题保留 |
| 仅原失败 lint 目标复验 | 80848a1c9e9940621c8ea583185fc5e186e09763 | PASS，exit 0 |

初轮源码指纹为 `c59f9cd932f45d6a169415ec92fc41a7fad205e8a7d6dc7cf5d77ab116157049`，
格式修正后为 `b77b8975b8f6c1a01ef997039010e7fe0b745eea253eb252769f113c61f299ef`。
两次检查均使用预先提交的 Git archive；五个格式修正文件的非 import AST 一致。
11项运行检查、typecheck 及历史通过项没有重跑，不宣称最终版本单轮全量通过。
检查覆盖并发去重、撤权阻止发送、来源过期、拒绝／丢回执、不回显原文及 fixture/live 隔离。

原检查启动器一次目录布局错误发生在测试开始前；最终只读证据采集器一次 SecretStr
解包错误也单独保留。两者均没有触发测试、付费请求或新的平台写入。

## 实际平台回执

| 原 Action | 飞书 reply message_id | 原 Outbox attempts |
| --- | --- | --- |
| 986d1e65-3c13-4797-8d60-c46063f22afc | om_x100b609cbcb204bcc1b342e594d4502 | 1 |
| 2b02ac31-a401-4866-b193-4c19c3b141ee | om_x100b609cbdd9f4b8c0683200d04a226 | 1 |
| ca28099f-b1ad-4718-bbef-1c987ecdb2c8 | om_x100b609d6b194ca4df896552e6a3844 | 1 |

三条输入的远端原消息 ID 不同，Owner 与 Bot 群目的地均精确匹配。原始只读观察保存了
Action/Input/Delivery、版本和原消息 ID 摘要，未把聊天原文、平台密钥或会话写入公开摘要。
桌面截图包含无关窗口，未复制到公开证据或 Git。

原始证据目录：
`evidence/super-agent-v1/SAV1-22/start-20261116T040246Z-f670fff6/reply-backend-20261116T043531Z-931a1969/`。
其中 `initial/registered/` 是原 JUnit／日志／result 的逐字节副本，
`live-reply/observed-result.json` 为首次两份回执，`live-reply/followup-observed-result.json`
追加记录第三份回执；`cleanup.json` 为清理记录。
历史失败、12080份受保护文件、`.env` 和用户文件均核对未变。

## 运行状态与剩余范围

本轮检查专属 PG／会话已移除，其他资源未动。为实际收信和回复保留专属接入 PG
`mgar-sa22-bot-edca22cd043e` 与接收器 PID 1307342；主库仍44份迁移，接入 PG 为既有50份。
本次授权到 **2026-11-17 12:04:22（Asia/Shanghai）** 自动到期；服务依赖本机／WSL在线，
自动重连没有启用，尚非云端常驻部署或72小时稳定性成绩。

新增模型调用0次、费用USD 0／已授权上限USD 2。Router、模型、Coding Worker、PR及发布
均未激活。H12 原 live_agent/MVP FAIL 保留，不把本次平台回复算作编码或修复成功。
本切片工程检查与实际固定回复有通过证据；完整 SA22 的六个门仍为 NOT_RUN，
真实编码／发布、200条路由评测、10个正向任务、72小时与5个工作日试用仍待执行。
SA23 未启动。
