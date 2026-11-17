# 逐例记录、时延与试用判定

本文件是实验记录约定；不存在本轮正式成绩。所有时间、ID与状态由实际原系统观察，
不能由模型答案补填。验收文件只存关联与评分材料，不裁决业务状态或维护第二账本。

## 逐例

每个case有固定case_id、attempt_id和目的。收到、拒收、未运行、known-invalid、unknown、
失败与通过分开。记录：输入／来源／Thread／Ticket／Run／Assignment／Session／epoch／
Job／Action／Delivery、当前policy/source/profile revision、基线与最终tree、diff、命令、
退出码、原始日志CAS、独立grader／公开测试hash、用量／预占／人工介入及失败原因。
无该对象的场景明确null，例如路由拒收没有Worker；不能编造ID证明链路。

一个实际编码工作项多次观察可用于状态／恢复场景，但每次评分要注明related_case和
复用的原Task。分别报告“场景8/10”“实际代码工作项x/5”“强制覆盖已观察x/y”。
所有任务的基础设施失败／取消／截断／unknown保留在原分母；拒绝无权限请求只计负例。

R组的结果必须由原Engine事实normalizer构造：case_id、status、action或null、目标、
receive/model_input/control/notification实际标记、decision/input/call引用和error。
缺字段／非布尔标记／重复case／未注册输出不能按成功行解释；缺失行补为missing计分。
模型自述“我没越权”不是观察，原would_create也不表示实际Task已创建。

## 时延

健康条件下分别收集至少20个新样本，不重复旧通过测试：

| 指标 | 起止边界 | 冻结目标 |
| --- | --- | --- |
| 入站提交 | 受控接收入口开始 → 原Gateway返回可证durable receipt | P95≤2s |
| 订阅内部投递 | 合法已提交输入就绪 → 原Mailbox持久投递 | P95≤2s |
| 空闲领取 | 原候选ready且槽位空闲 → Assignment领取提交 | P95≤30s |

使用同主机单调时钟配对，另存UTC与原ID；跨进程保存观察端点和时钟误差。平台网络、
模型首token／总时长、镜像／依赖准备及满载排队单列，不混入健康内部SLO。
少于20只报n／中位数／最大，不给不稳定P95。缺测、无空闲槽位或权限阻断单列。
当前soak的`ingress_and_reply_seconds`是合并时延，不能冒称这三个内部指标均已测到。

## 故障与资源

注入前绑定：试验ID、明确自有PID及启动标记、容器ID／project／assignment标签、专属
PG/CAS/Redis和预期checkpoint。检查失败就不注入。保留kill／断线／ENOSPC等实际OS
证据、故障前后原状态和回执，不直接UPDATE status。清理只针对本次精确归属对象。

核账只读原usage_calls/router_calls与SA07汇总；每个物理调用的known/unknown/预占
均追踪到原记录。恢复不生成新钱包，过午夜不退unknown。沙箱／API／Source观察进程
按职责记录心跳与业务进展，心跳本身不是修复进展。

## 真实72h与本人反馈

当前受控连续段结束且证据合格后，另建真实阶段start记录，不追算之前零散Bot在线。
真实72h与≥5工作日反馈可重叠；工作日按本人实际使用日记录，不能用5次消息代替5日。
无消息时也记录0条、实际空闲调用／费用。没有事件不唤醒模型。

每日记录有效请求、漏接、误触发、多余通知、是否有用及原因、人工次数／原因、费用、
unknown和未完成任务。未反馈不是默认满意。沿用原本人反馈表，试用者仅本人。
开始前核对来源、Owner session、模型／编码授权期限和保留策略，明确同范围续期记录。

最终输出升级／保留实验／回退决定，并逐项列六门与具体支持范围。publication可保持
NOT_RUN且只交付待审；H12真实质量FAIL不因本实验或后台采集改为PASS，SA23不自动启动。
