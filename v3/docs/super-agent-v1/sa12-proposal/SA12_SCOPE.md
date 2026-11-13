# SA12 调度与 Assignment 资源权威草案

状态：DRAFT_NOT_APPLIED。用户接受SA11独立闭环后授权本卡；共享范围和0044仍待新固定确认。
本稿不启用Worker/平台/模型/付费/发布，不实施SA13/14或导入H12。

## 原权威与启用

PG及原Engine同一advisory lock、RLock、connection是唯一业务写者。多个调度进程经同一
Engine的类型化入口竞争，不各建Engine/业务连接。新增队列、Session、Assignment、
operation和公平事件只描述原Task/Run的执行所有权与资源；原Ticket/Run/Mandate仍是
唯一目标、生命周期、费用与完成状态。没有第二份question、Source正文、模型输出或钱包。

默认disabled、无Session、无Worker driver、无模型。主人显式配置后只登记已有且获准的
Run，不从Source消息自行建Task或恢复终态。Owner登记不等于新增ExecutionGrant：
实际工具/模型/写入仍受原Mandate、Source策略、真实能力门及付款许可约束。
Source origin可选；有Source时仅接受当前canonical引用及授权快照，原目标精确核对，
权限/actor/human优先级由受信事实推导，模型/正文/Worker不能自称human或扩权。
首版仅本服务器既有project，组织/项目配额同时约束；不宣称开放SA24多租户。

## 事务领取与唯一epoch

Queue使用run/generation/request_id幂等；有效占用Task、Run、Session与workspace唯一。
claim在原短事务中校验当前配置、Session、Task/Mandate、Source/身份、精确epoch、
当前共享预算和queue/slot；公平挑选资源fit项，先占preparing/slot/CPU/内存再物化目录。
每代入队、每次Assignment使用新的不可变quota key，重试同一次才复用，释放不退款费用。

claim内通过原begin_run只推进一次epoch。受管原图再次begin_run仅在持当前可信Worker
上下文、有效Assignment/lease时返回同一已领取Run；不能二次推进或由旧API跳过领取。
begin_run内的原heartbeat也会调用_run(new epoch)，因此服务必须为同一持锁事务内
已鉴权/占额的精确queue/run/session/old-new epoch建立内部claim上下文，允许这一次
嵌套检查；不能把无Assignment的所有受管Run都放行。HTTP/Worker不能制造此nonce。
未受管旧Run维持原路径。旧API supervisor仅跳过受管Run，Owner list_runs仍显示全部。

资源上限：活动slot<=2、preparing<=活动slot；CPU池<=4000m、内存池<=4096MiB；
每Assignment CPU100..2000m、内存64..2048MiB；queue<=256、单source slot<=2。
整体准备期限默认120秒、最多600秒，不因heartbeat延期；preparing和draining均保守占准备容量。
PG租约默认30秒、最多120秒；Session期限最多86400秒。Heartbeat仅续未过期当前租约，
不得越Session/配置期限，不复活过期或旧epoch。

公平依据PG事件，可从PG重建。人类与自动化均有积压时，最多每两次claim给获准人类
一次机会；同层按project/source最后服务序与原入队年龄稳定选择，CI新桶不能淹没人类。
资源不fit或权限失效明确保留原因，不把缺预算当模型已评估或任务成功。

## IO、Broker与最后发送门

H03 WorkspaceRegistry/TaskWorkspace只取固定已验文件。Registry/operator root由受信
服务配置；API不接受宿主路径或raw load ID。Workspace ID由服务器生成，Task副本独立，
不暴露H03未经过CapabilityAuthority的工具registry，不调用apply_back。H03原snapshot的
workspace_id是registration级；私有adapter必须冻结Assignment UUID到registration/hash、
task_id=Assignment UUID、真实目录/provider generation的映射，以actual H03 tree签署
准备receipt，不把registration ID当分配唯一目录或把Broker裸tree当源码验证。

H04 CapabilityLease/Authority和隔离约束为基础。候选增加固定H06的launch/inspect/stop
Broker切片，复用同一执行内核，不导入CommandSession/H09/H12或对方迁移。SA12需要的
持久容器身份、最终派发guard和不确定inspect区分单独提案，不称原H06验过新扩展。

PG先保存确定的operation/container身份和请求hash，再在事务外准备/执行。派发临界区
保留原Engine mutex：最新PG lease/Run/Source校验提交后，穿过实际docker start入口再
释放，阻止已提交撤权与旧请求之间的空窗。不得在PG事务里联网或执行容器。

H03读写及Broker每个新操作重查当前Session、Assignment、workspace、epoch、PG时间、
Source/原Mandate。真实模型/工具成功仍由原Engine/Verifier决定；原财务settlement不
因lease过期被抹掉或误退款。Broker裸tree_hash不足证明代码树，需绑定实际H03 tree。

## 进程与资源收尾

Assignment只有preparing/active/draining/released资源阶段。过期、撤权、Worker失联或
Controller重启先fence原epoch并转draining；在途操作和unknown保留，停止/检查IO在
事务外，由受信Broker事实提交回原Engine。启动恢复沿用原recover先pause与unknown
规则，新增hook只收敛Assignment资源事实，不自动resume。

Worker说finished/waiting_user不能证明无进程。requested/running/unknown operation、
原read_jobs queued/running、清理未确认都阻止release与同目录重用。只有精确绑定的
可信quiescence事实且无未完Job才幂等释放slot/CPU/内存；等待用户保留Task、Run、Source
游标、checkpoint及费用。daemon/transport错误不是进程不存在，不能当释放证明。

Redis仅缓存，清空后从PG重建，不重放未知模型/工具请求。SA14完整异步输入与安全点
协议留待后卡；本卡operation事实只用于执行资源占用与已知/未知收尾。

## 管理与Worker接口

前缀`/api/v3/source-scheduler`，拟15个metadata接口。GET/state可选run_id，仅在Owner
授权后返回{capacity,target}中的原目标引用/当前epoch/稳定hash，无正文或host路径。Owner沿用cookie、Origin/CSRF、
事务内当前会话与no-store；Worker只使用独立、可撤销Session bearer，不下发主人cookie。

| 主体 | 方法 | 路径 |
| --- | --- | --- |
| Owner | PUT | /config |
| Owner | GET | /state |
| Owner | POST | /queue |
| Owner | GET | /queue |
| Owner | POST | /queue/{queue_id}/cancel |
| Owner | POST | /sessions |
| Owner | POST | /sessions/{session_id}/revoke |
| Owner | GET | /assignments |
| Owner | GET | /assignments/{assignment_id} |
| Owner | POST | /assignments/{assignment_id}/drain |
| Owner | POST | /assignments/{assignment_id}/reconcile |
| Worker | POST | /worker/claim |
| Worker | POST | /worker/{assignment_id}/heartbeat |
| Worker | POST | /worker/{assignment_id}/ready |
| Worker | POST | /worker/{assignment_id}/report |

只处理CAS、原目标、资源、epoch与可信operation引用。Worker不能挑队列跳过公平，
也不能提交正文、host路径、命令、factory、支付授权、自证身份或quiescent布尔值。
ready/report必须匹配服务端可信准备/进程事实，不直接改变业务完成状态。

## 首次正式验证范围

正式接线后再注册并执行真实SAV1-12目标：PG竞争/事务回滚/实际多进程命令、旧lease/
epoch/Session/Source撤权拒绝、两份独立H03可写目录、CPU/内存/preparing/slot/共享quota、
进程/Job未结束禁止释放、真实退出后一次释放、人类与CI公平、PG恢复与缓存重建、
实际隔离Broker及新身份/inspect/发送边界。实际调用前严格禁止未知进程重放。
首次静态/正式成绩均需新代码绑定；修复以后仅复验原失败，保留此前所有红灯。
独立/真实平台/真实模型/真实编码/外发/发布/长期门另记，SA13不自动启动。
