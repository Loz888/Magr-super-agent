# SA12：Scheduler、Assignment 与资源租约

状态：IN_PROGRESS。共享范围已按 Harness 固定提交
`298d4c5c5123a5c26c34a9673d676516d1503f84` 第37节确认；本地首次正式检查尚未执行。
本卡不启动 SA13，也不授权真实平台、模型付费、外发、发布或常驻运行。

原 Engine 的 advisory lock、互斥锁和同一 PG 连接仍是唯一业务写者。正式0044只增加
配置版本、队列、Session、Assignment、操作和事件七类资源事实，复用原 Task/Run/Mandate
和 SA07 queue/slot 配额，不复制任务、正文、执行结果或财务账本。旧41份SQL保持原字节，
0037/0040不导入，0045保留给Harness。SQL/补丁原稿及其历史草案状态保留，当前落地状态
由本文件、正式产物清单和逐次证据说明。

默认没有配置、Session、driver或监督线程。Owner显式配置现有项目，审查原Run精确目标后
入队；Worker使用独立可撤销bearer，不能用Owner cookie替代。15个接口只传有界资源和
身份引用，不接受命令、路径、正文、优先级、身份自证、付款或静默布尔值。
GET `/api/v3/source-scheduler/state?run_id=...` 返回容量与原目标hash，没有宿主路径或正文。

claim先在原事务中核对当前配置、Session、Run/Mandate、Source和共享配额，再推进一次
原Run epoch、占准备容量并保存确定的workspace/operation身份。目录和进程IO均在事务外。
受管Run不能再从旧自动supervisor跳过领取；可信Worker上下文重复begin不再递增epoch。
队列、人类/自动化轮次、来源服务顺序和原入队年龄均存PG，Redis不是领取权威。

最多两个槽位，总CPU4000m、内存4096MiB；单项CPU100–2000m、内存64–2048MiB。
准备和draining持续占容量，准备总期限不因心跳延期。租约过期、撤权或旧epoch拒绝新执行，
先fence并保留资源，不能把超时当作进程已消失。Worker报告finished/waiting_user只是资源
建议；可信静默回执、原read_jobs及所有待定操作核对完成后才释放配额，原业务完成另判。

工作区只取固定H03文件；每个Assignment映射到独立Task副本，registration ID不充当
唯一Task目录。实际准备回执绑定H03内容树，模型或HTTP不能选择宿主路径、调用apply_back。
Broker只取固定H06文件加已确认的绑定hook；不导入H05/H09/H11/H12或CommandSession。
既有H02 contracts仅增加兼容的 `ExecutionRequest.execution_epoch=0`，原版本保存在Git及
清单历史中，其余五个H02固定产物保持原字节，不把这一扩展说成六个文件全部未变。

受信服务器可显式构造 `H03SchedulerResourceDriver` 和 `RuntimeLeaseSupervisor` 做
fixture资源试验。执行命令、镜像、grant及注册由服务器冻结；当前原调查grant仍只读，
Worker写权限与真实Coding桥留待SA13。构造和import均不自动启动线程或进程。
监督器独立检查PG lease、Source和持久命令deadline；重建driver不丢失操作身份或延长期限。

启动先提交确定的容器名、全部owner标签和请求hash；create后，在原Engine mutex内严格
核对不可变ID/owner/created状态，慢inspect完成后再读当前PG时间与权限，随后按不可变ID
start。事务在Docker IO之前结束。恢复仅查询原身份；未知结果不重发。daemon错误不等于
missing；停止仅删除精确匹配的ID，严格确认不存在并封存工作区后才能签发静默事实。

首次检查使用 `make lint`、`make typecheck`、`make verify TASK=SAV1-12`。测试将区分
受控无进程文件driver、真实H03目录、真实隔离Broker与真实SIGKILL，不能相互替代。
修复后只复验原失败项；旧通过与新通过逐版本记录。检查及独立验收未完成时本卡保持
IN_PROGRESS，未实测的真实、语义和长期门保持NOT_RUN。
