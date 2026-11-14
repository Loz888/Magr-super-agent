# SA15 可鉴权 Chain of Recall

状态：SA15-I01独立未通过；45a79cf仅原失败1项本地PASS，整卡IN_PROGRESS，等待独立复验。
原110正式／89准备分版本证据保留，不重跑。见[本次修复](SA15_I01_REPAIR_20261114.md)。
源码与静态结果绑定限制见 [工程交付](SA15_ACCEPTANCE_20261114.md)。
共享所有权是 Harness `e0432f0e289de868a5956339aeec76db71bd0efe` 第40节。
Engine/API 两文件按已确认补丁落地，补丁 SHA256
`152b61e72bca0e9e0546c8e8295002f131cfe3a8c94c5ba8e452111f5d33cad9`。
未新增 SQL，也未预占0048；原44份迁移不变。SA16未启动，H12由另一CLI负责。

## 接口与调用

前缀 `/api/v3/source-recall`，默认只返回无正文元数据，所有响应 `Cache-Control: no-store`。

| 调用者 | 方法/路径 | 请求契约 |
| --- | --- | --- |
| Owner | GET /capabilities | 当前原控制台cookie |
| Owner | POST /resolve | RecallResolveRequest |
| Owner | POST /walk | RecallWalkRequest |
| Owner | POST /page | RecallPageRequest |
| Owner | POST /search | RecallSearchRequest |
| Worker | POST /worker/resolve | WorkerRecallResolve |
| Worker | POST /worker/walk | WorkerRecallWalk |
| Worker | POST /worker/page | WorkerRecallPage |
| Worker | POST /worker/search | WorkerRecallSearch |

Owner 的 POST 读取沿用原 Origin/CSRF，服务内部在拿到原 Engine 事务后再次验证当前cookie。
Worker独立bearer绑定当前原Coding Run、Assignment、Session、epoch和Mandate；Owner cookie、
operator token和模型自报actor不能替代Worker凭证。首片Worker跨任务检索限同一当前获准安装/会话。
Source requester与context_provider、Receive/model_input和canonical head均重新核对。

`path.refs` 是1–5个 `{kind,id}`，ID为规范UUID，artifact为小写SHA256。来源根为原input_id；
thread/ticket/run/job/evidence/gap也是原PG身份。memory/review/delivery/artifact不能作为无来源根。
每个跳转必须存在原关系，并逐个验证目标权限。例：

```json
{"path":{"refs":[{"kind":"run","id":"<original-run-uuid>"},
                       {"kind":"artifact","id":"<referenced-report-sha256>"}]},
 "max_bytes":2048}
```

这里占位符仅用于说明，请使用真实当前对象ID；不接受宿主路径、URL或任意CAS hash下载。
只读工具 `source_recall`、`source_recall_walk`、`source_recall_page`、`source_recall_search`
由私有 `SourceRecallTools` 显式服务端注入既有 Harness ToolContext。构造不启动模型/Worker；
本片没有改固定Harness kernel、Broker或SA13/14执行器，也没有默认向模型开放这些工具。

## 权威、关系与正文

复用原Source、TaskCorrelation、Subscription、Scheduler、Async Job、Thread/Ticket/Run、
Evidence、Action/Outbox和ContextEdge的PG主键/外键。关系索引是有界只读投影，不新建Graph表、
授权库、正文缓存或业务写者。Review在此对应已有Action，Delivery对应已有Outbox回执；不宣称
后续SA17/18多渠道通知和Review Bundle交付已实现。unknown仍为unknown，不从摘要推断完成。

所有PG读取沿唯一Engine持锁连接与短事务；每语句1秒、锁等待250毫秒、操作规划5秒、候选64。
遍历最多4跳/32节点/128边/16KiB，页128–4096字节保持UTF-8边界；CAS本身保持原2MB上界。
Source正文从原加密保留区读取，不复制到Memory/CAS。来源删除/编辑/撤权后只保留最小无正文事实。
正文到期后，只要当前Receive、身份、canonical head与元数据保留仍有效，已知Task仍可查真实状态；
这不恢复Source原文或派生产物的正文读取权。词面搜索不能从已删除正文猜测旧任务。

CAS IO在SQL事务外。释放内容前重开原短事务，重验caller、路径、全部来源依赖、当前版本/权限
及原内容引用。派生内容要求全部已知Source依赖仍可读，包含历史关联与实际已应用输入；不能
通过一条公共ContextEdge或公开父任务绕过私密引用。旧关系不恢复当前读取权。

Memory沿原新旧表、kind/detail解码与生命周期；撤销、stale或跨scope拒绝。摘要只作不可信数据，
不能增添执行/付款/发布权或任务成功事实。Knowledge证据继续检查原snapshot/chunk/document ACL
和精确来源事实。没有可验证当前权限的其他R4数据源内容在本片保守拒绝；不调用额外数据/模型服务。

分页游标为服务端AEAD密文，绑定caller、query/path、内容/version、offset和短TTL。每页重验权限；
游标不携带明文私密候选ID。来源失效、cookie登出、Worker epoch/权限变化、跨对象/篡改/过期均拒绝。
Source gap明确表示历史不完整，返回最小状态，不宣称已经补齐。

## 检索与质量边界

search使用当前可见Source及其原任务关系做有界确定性词面匹配，默认最近7天；可显式提供成对UTC
时间范围（最多31天）、安装与会话过滤。精确时间范围应由调用方提供；它不是自然语言日期解析器。
“昨天的登录问题怎样了”沿关键词与原关系返回当前Task状态；零或多候选要求提供任务ID、会话或
更精确时段，不随机选任务、不发送全天聊天。首屏解析后的实际UTC起止时间保存在认证加密游标中，所有续页恢复原范围，不随当前时钟漂移。
缺少实际时窗的旧搜索游标拒绝续用，需重新开始查询；显式时段沿原范围语义。
分页查询每次重验当前权限，不用旧候选缓存或已冻结时窗恢复旧授权。

冻结合成集为 `fixtures/super-agent-v1/sa15/retrieval-quality.json`，12例分别核对授权任务ID、真实状态、
歧义/未命中与时段排除。逐例成绩随本卡证据交付；这只是固定词面/时段检索工程分母，不是付费模型、
真实团队或泛化语义质量。H10固定c140dad的F01/F02独立范围审计保留，其他OPEN不因本卡变PASS。
已有long_context字节相同，未导入H10文件Registry或任何H12活动实现。

## 验证与运行门

准备候选5a172ff的89项私有契约、planner、opaque cursor检查及其静态PASS保留，不重复执行。
正式入口 `make verify TASK=SAV1-15` 只注册本卡新PG/API/ToolContext/质量目标。
失败按原用例/目标定向修复重测，保留每轮源码commit、不可变archive、原始日志和失败记录；
不将分版本证据声称为修复后单轮全量PASS。

真实平台、付费、外发、发布及长期运行均为NOT_RUN；本片不开监听、建Worker、付款、发消息或发布。
独立通过与用户放行之前不启动SA16。
