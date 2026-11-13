# SA10 跨来源关联与原任务查重

状态：IN_PROGRESS，本地正式集成和工程复验已完成，等待独立复验。当前源码候选`dcea520`；正式201项首轮200PASS/1FAIL，唯一守卫兼容性修正后仅原失败1PASS，未重复其余200项，也不称最终源码单轮201PASS。详见[工程交付](SA10_ACCEPTANCE_20261113.md)及[机器摘要](accept/SAV1-10/accept.json)。SA09-I01已有独立原6项PASS闭环，当前元数据已同步[关闭记录](accept/SAV1-09/i01-independent-closure-20261113.json)，旧独立FAIL保留。SA11未启动；真实平台、付费、外发、发布、语义质量和长期门均NOT_RUN。

## 所有权与集成状态

Harness固定`0a335a14d8ce37703b5cfe0eaa7960d4ac571fc7`第34节实际确认0041、私有模块和SA副本最小Engine/API接线；SQL、补丁及请求哈希均匹配。`0041_source_correlations.sql`已按原草案字节进入正式migrations，经原`make migrate`安装；当前39份checksum一致，无0037/0040导入。原Engine.__init__仅组合服务，原api.py仅注册六个管理接口，实际字节等于已确认补丁。没有修改或导入Harness活动文件、H12或产品实现。

新SQL只有5张无正文事实表：当前native资源观察、correlation head、不可变版本、唯一资源claim及固定revision的输入链接。canonical input/source/project及原Thread→Ticket→Run复合外键保持；没有指向可清理source_inbox的永久外键。deferred约束使事务内占位必须在commit前具备完整原目标、native claim和输入链接；历史不可更新或删除，不复制业务状态机或费用账本。

## 独立契约与明确资源

新契约为source-correlation-v1/source-correlation-snapshot-v1。SourceScope、不可变repository_id、资源kind及资源ID共同决定resource_key；仓库名字、标题、相似度、裸编号、同群/同topic和代码SHA都不能替代资源身份。一个输入可以有多个unit，topic是上下文证据，不能唯一决定一个Task。

GitHub资源只从当前授权的加密canonical正文经GitHubResource校验提取，核对当前安装设置和完整scope。正文、URL、客户端TargetRef.allowed及模型SourceDecision不提供权限。跨来源别名需要当前主人明确审阅双方可读输入；每次读取仍逐边检查当前Source/identity/policy/head/TTL。旧别名固定在签署revision，不能通过claim新head静默换到另一个目标。

CodeRef单独绑定head/base/head-repository/tree及CI run/attempt/check。相同PR可以继续属于原Ticket，但force-push或CI attempt改变不能继承旧代码验证或审批；缺少精确tree时verification_reusable=false。SA09 v1快照、tag、模型pipeline、预算hash和I01发送栅栏不变。

## 同事务查重与纠正

管理接口只操作owner明确签署的context关系，不创建Worker、Mailbox投递、模型调用或外发。唯一的原任务创建入口是受信runtime内部claim_or_create(...,create_target=None)，HTTP不接受factory。默认没有工厂；当前工厂只允许queued/unstarted的原fixture Target，真实Source编码桥仍留SA13/16。

该端口先在原Engine事务认领唯一资源，再仅由赢家调用factory(original_connection)，后验读取实际原Thread/Ticket/Run/Mandate并重新核对Source和会话。工厂和claim/版本/链接共同提交或回滚，重复不再次调用工厂。单PR CI同时认领PR parent与CI自身，避免换一个CI key制造第二Task；多个PR的CI不能随机选择其中一个。没有PR证明时也不按SHA猜同一问题。

纠正必须提供当前snapshot_sha256及expected_revision，并重新构造当前输入和目标。换目标前检查旧Ticket所有attempt、epoch/status、费用、unknown、读取、工具、repair及Action事实；已执行工作不后台迁移到新授权目标。原事件、原决定、旧版本和原业务目标保留，旧输入链接不跟随新head隐式改写。

## 管理接口

以下为已由正式应用注册的管理接口，沿用cookie、Origin/CSRF、事务内会话复核和仅本前缀的no-store。请求仅接受现有引用及精确CAS，不接受正文、资源证明、身份、付费、执行或factory字段。

| 方法 | `/api/v3/source-correlations` 路径 | 行为 |
| --- | --- | --- |
| POST | /resolve | 只读重建当前快照及明确/未定结果 |
| POST | /links | owner审阅后建立context关系 |
| POST | /links/{id}/correct | 受限CAS纠正，保留历史 |
| POST | /links/{id}/remove | 追加撤销版本，保留claim防重复建单 |
| GET | /links/{id} | 当前可读的关系及新鲜快照 |
| GET | /inputs/{input_id}/links | 固定revision的历史链接，最多100项 |

## 当前验证证据

独立e9ee378的原两项正式守卫FAIL完整保留。落实正式0041及共享接线后，`191ed23`原`make verify TASK=SAV1-10`实际执行201项：200PASS/1FAIL，0error/skip，其中原SQL守卫PASS及其余199项首次正式检查全部PASS。

唯一剩余接线守卫在确认真实Engine及原PG连接后，因FastAPI的`_IncludedRouter`没有path属性而中断。只将该失败守卫的枚举过滤改为`getattr(route,"path","")`；原六个精确路由/方法、Engine和原连接断言AST全部不变，生产源码未改。固定`dcea520`后仅该原失败节点1PASS，200项未重复，原201轮FAIL不改。最终源码指纹`dd47a56701afcee6a8257e1ec152d48556cc7feea6d253a6e324c4c0cd326e87`复验前后一致。

24项API首次正式检查全部使用原create_app；移除私有测试临时注册组合，所有实际测试函数和业务断言AST保持。之前私有组件、收集201及静态结果均作为各自版本的历史证据保留；本次只对新接线/夹具/目录变更做定向静态检查，没有重跑历史整库lint/typecheck或组件流水线。

原始证据位于`evidence/SAV1-10/20261113T034518Z-84ae9b20/`和`evidence/super-agent-v1/SAV1-10/integrate-I01-20261113T034036Z-0de9b080/`。两项原失败守卫及199项首次检查的逐例账本、命令、JUnit和各版本源码绑定均在新工程摘要引用。独立复验前本卡保持IN_PROGRESS。
