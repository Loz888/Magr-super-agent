# SA10 跨来源关联与原任务查重

状态：IN_PROGRESS，私有组件验证完成，等待共享范围固定确认。首次整卡目标已注册并收集201项，尚未执行正式集成验收，详见[准备摘要](accept/SAV1-10/preparation-20261113.json)。用户明确授权SA09-I01失败修复检验成功后继续SA10；该条件已由原六项定向6PASS满足，原独立NOT_PASSED及修复候选待独立状态保留，见[修复记录](SA09_I01_REPAIR_20261112.md)。SA11未启动；真实平台、付费、外发、发布、语义质量和长期门均NOT_RUN。

## 所有权与集成状态

Harness固定`f554937eb0c692c54ff6fa2d2cede67f91e85a10`第33节已占0040，本副本不导入或占用该编号。SA10拟0041_source_correlations.sql；SQL和仅两文件接线已经形成[具体协调请求](sa10-proposal/ownership-request.txt)，当前等待新的固定Harness确认。SA09第32节不覆盖本次新表和接口。确认前，正式migrations、主库和共享Engine/API保持不变；组件测试仅在自己的临时PG显式安装SQL草案。

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

以下为待共享注册的私有接口，沿用cookie、Origin/CSRF、事务内会话复核和仅本前缀的no-store。请求仅接受现有引用及精确CAS，不接受正文、资源证明、身份、付费、执行或factory字段。

| 方法 | `/api/v3/source-correlations` 路径 | 行为 |
| --- | --- | --- |
| POST | /resolve | 只读重建当前快照及明确/未定结果 |
| POST | /links | owner审阅后建立context关系 |
| POST | /links/{id}/correct | 受限CAS纠正，保留历史 |
| POST | /links/{id}/remove | 追加撤销版本，保留claim防重复建单 |
| GET | /links/{id} | 当前可读的关系及新鲜快照 |
| GET | /inputs/{input_id}/links | 固定revision的历史链接，最多100项 |

## 当前验证证据

契约/纯规则102项PASS；初始collection和静态失败及对应失败范围修复均保留。SQL草案实际临时PG首轮4PASS/10FAIL，修正失败项的原工厂fixture后仅10失败PASS；SQL字节未改，不合称单轮14全绿。

服务首轮18项为14PASS/4FAIL；四项均为测试布置与既有约束不符，未改生产逻辑或业务断言，仅原失败4项复验3PASS/1FAIL，再只剩余1项PASS。新增CI parent及工厂边界首次6项为4PASS/2FAIL，仅两项夹具的策略保存返回字段修正后定向2PASS。API私有组合首次24PASS；测试临时注册私有模块，不代表生产API已接线。

首次整库typecheck为249源码文件PASS。首次整库lint仅两个文件有5处格式失败；只修改API泛型标注和SA09复现镜像括号/字符串折行，随后仅这两个失败文件lint PASS，未重复typecheck或已通过pytest。SA09镜像整文件AST相同，原独立探针和SA09生产修复模块未改。API的24PASS绑定格式修改前源码，后续正式集成不借用该成绩冒充新的执行。

`make verify TASK=SAV1-10`现有8个真实测试文件，collect-only共201项（166项SA10、35项直接SA09依赖），不是201PASS。迁移14项正式镜像保持原整函数及断言AST，另加两项尚未执行的正式迁移/Engine/API接线guard，防止仅测试草案和私有组合就误报整卡通过。实际运行须在固定Harness确认、正式0041和共享接线完成后进行；不能绕过已知未完成的集成前置。

原始证据位于`evidence/super-agent-v1/SAV1-10/`。所有失败、命令、JUnit、源码范围和未实测门分版本保存；后续修复仍只复验原未通过项。
