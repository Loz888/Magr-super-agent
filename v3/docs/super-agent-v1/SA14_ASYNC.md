# SA14 异步输入与 Job

状态：本地受控工程已闭环，等待独立验收；整卡 IN_PROGRESS，SA15未启动。
[工程交付](SA14_ACCEPTANCE_20261114.md) · [分版本成绩](accept/SAV1-14/accept.json) · [独立核验请求](SA14_INDEPENDENT_REQUEST.txt)。

原 Engine 持锁 PG 连接管理同一 Thread／Ticket／Run／Mandate／Assignment 上的输入和原资源 Job。
新输入按 revision 持久化为 received／applied／blocked；只保存 Source 引用，不另存正文。
Owner 的有类型 deny 或可信当前人类精确 Ticket 控制只能收紧原许可；CI、上下文、引用不授控制权。
安全点形成不可变视图，实际模型／工具／编辑／命令边界再验当前权威，完整丢弃旧模型回复。
受控模型 prepare／短 dispatch／poll 分开，命令复用原 bound Broker，等待期间不占 Engine 锁或SQL事务。
原子文件批次收尾保留真实树和原回执，新限制阻止后续不兼容操作；在途费用继续原账本真实结算。

第39节 c711d545 已确认并按原字节应用0047和三文件hook。正式44份迁移，12个 metadata/no-store 路由；
当前受控范围仍只支持既有公开fixture_git、纯函数Python及原H03／SA12 bound H06／固定H11。
没有引入H09文件Store／Inbox、另一Task或钱包，也没有导入H12。默认无Runtime／Worker／监听或后台线程。

正式首轮61项26PASS35FAIL；后续仅失败项复验，新增原子编辑边界首次通过。62个独特目标跨版本闭环，
并非最终源码单轮62PASS。私有65／SQL草案29的历史证据与所有失败保留。等待独立验收，不标DONE。
H12验收由另一CLI负责；本副本固定H12真实门FAIL、其他真实／付费／外发／发布／长期门NOT_RUN。

[冻结范围提案](sa14-proposal/SA14_SCOPE.md)和[提案哈希](sa14-proposal/proposal-manifest.json)保留原字节；
提案中的“等待确认”是当时状态，实际确认见[第39节固定记录](sa14-proposal/ownership-confirmed.json)。
