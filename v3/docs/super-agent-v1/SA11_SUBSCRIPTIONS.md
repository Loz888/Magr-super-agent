# SA11 动态订阅与 Source Mailbox

状态：IN_PROGRESS，本地工程已具备独立验收条件。固定Harness `d0af0747` 第35节的
0042、七表、11路由和四文件接线已落地；`a9b02bfe` 第36节确认0043前向修复。
当前41迁移，原40 SQL原字节保留。首轮351项309PASS/42FAIL，随后仅失败范围逐轮
42→40→26→1复验，各新增2/14/25/1PASS，剩余0；没有重复原309或中途通过项。
各版本与原始红灯见[交付报告](SA11_ACCEPTANCE_20261113.md)和[机器摘要](accept/SAV1-11/accept.json)。

## 输入、授权与原目标

新协议为source-subscription-v1/source-intake-v1。主人以现有cookie、Origin/CSRF和
事务内会话审阅当前SourceCorrelation、明确资源/原生话题/reply链、现有Task/Run及
用途和TTL。每条新输入重新核对Source、身份、Receive、策略版本、正文head/TTL、
原目标与Mandate；同名、标题、整群或跨仓同编号都不能自行建立任务归属。

primary/context_only/observer是输入用途。primary还要求当前人类Task控制者及精确
follow_up许可；只读成员、机器人和历史输入不会升级控制权。不同问题存在歧义时
保留原因，不随机选择。关闭或终态跟进可查询原状态，不自动创建/恢复Run。

订阅绑定原Ticket/Thread/Mandate和明确Run；同Run新的epoch可重新准备页面，旧页面
无法跨epoch消费。新Run只允许主人显式rebind到同Ticket的已存在且获准Run，不跟随
latestRun。订阅selector的期限与旧seed正文TTL分开，旧正文失效不会被续期复活。

## 原Engine下的引用投递和消费

订阅、版本、路由观察、投递、intake、消费及handback均是原Engine同一持锁PG连接
上的新增事实，不复制Task业务状态机。投递分配原Thread.latest_seq，不推进其
revision；Source各lane拥有独立的引用消费游标，旧Mailbox/Run.input_revision及图
快照的语义保持。

新canonical接收事务内捕获投递和容量/歧义/关闭事实，提交后才ACK；普通重复/alias
不会自动激活旧输入。FastPath在已验证控制路径后检查当前订阅，已投递或待恢复的
输入不成为新任务语义candidate。原SourceEval在reserve前与I01发送前仍重验该链，
没有模型调用、账本重置或unknown重发。

页面有引用/数量/字节上限，准备与ACK分开持久化。主人可对queued当前epoch处理引用元数据，原Run开始后epoch提升使旧页失效；回执不表示工具或约束已应用。ACK重验页hash、游标、当前
Run/epoch、订阅head和每个输入事实。准备后撤权、编辑或过期会阻止旧ready页消费。
新blocked页面可确认拒绝元数据，以免无效输入堵塞积压；确认不代表读取旧正文、
执行指令或应用工具约束。只有当前授权的ready输入能短暂物化到内存，observer不能
物化Worker正文；所有持久事实、旧question和graph checkpoint均不接收Source正文。
真实Worker消费/发送的接线与安全点语义仍属于SA13/14。

handback保留原canonical版本和决定，追加一次needs_routing或loop_blocked待办，按
问题/时窗限制循环。不直接运行Eval，不改写旧FastPath不可变决定或重试unknown。
后续Router适配读取这些引用时仍需相应版本与授权。

## 管理接口

前缀`/api/v3/source-subscriptions`，全部为现有主人管理入口、no-store，HTTP只处理
引用/CAS/TTL及元数据，不接收正文、身份自证、factory、付费或执行授权。

| 方法 | 路径 |
| --- | --- |
| POST | /subscriptions |
| POST | /subscriptions/{subscription_id}/renew |
| POST | /subscriptions/{subscription_id}/rebind |
| POST | /subscriptions/{subscription_id}/unsubscribe |
| POST | /subscriptions/{subscription_id}/handback |
| GET | /subscriptions/{subscription_id} |
| GET | /sources/{source_id}/subscriptions |
| POST | /dispatch |
| GET | /runs/{run_id}/mailbox |
| POST | /runs/{run_id}/prepare |
| POST | /runs/{run_id}/ack |

默认没有订阅、监听器、Worker或模型Provider。SA12未启动，独立验收及真实平台、
编码、付费、外发、发布、语义和长期门仍NOT_RUN。卡内失败、原始命令和各版本结果
保存在`evidence/super-agent-v1/SAV1-11/`，不把SQL安装检查或历史通过写成整卡PASS。
