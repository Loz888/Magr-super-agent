> 当前状态：原候选独立验收发现 SA07-I01，未获放行；新修复候选见 [SA07-I01修复报告](SA07_I01_REPAIR_20261112.md)。以下保留原交付记录，不能作为当前独立通过结论。

# SA07 本地工程交接（待独立验收）

实现提交 `6fd95819883ed923d963e5d89858917a2fa3eab8`，契约 `super-agent-shared-budget-v1`，迁移0036；完整注册1118PASS，源码指纹`2d90679ae5310a8b12db4d4bc508f3521fb122ea3ac507df48d074291f19f2e7`。正式范围与未实测门见 [交付报告](SA07_ACCEPTANCE_20261112.md)。本文件仅本地准备，没有发送/对方接收或独立验收结论。

原Engine、RouterBudget、RepositoryAnalysisStore在原账本INSERT同事务调用`engine.project_budgets.reserve(conn, ledger_kind, call_id, price=PriceQuote, tokens=upper_bound, component=...)`。ledger_kind为usage/router；component为router/worker/verification/recall/subtask。配置组织/项目共同限制，Run只是子约束，失败连同原预占回滚；原结算函数不改。unknown金额/Token跨窗继续占款；缺历史Token上界保守拒绝，不伪造实际费用。

`quota(conn, kind, operation_key, units=1, source_id=None)`覆盖candidate/source/notification/queue/slot；幂等键相同参数仅占一次，参数冲突拒绝。queue/slot仅在明确结束/取消事实后`release_quota`，释放不允许重放旧key。Source速率与原通知审批/claim已接线，candidate/queue/slot仍是供后续Scheduler使用的内部端口。

Harness固定cb422c5第30节的所有权范围已核对。本卡未导入Harness PersistentBudgetLedger，不声明其已进入共同预算；0037归Harness，后续接线须固定契约并在本副本重验同事务、原调用ID/价格/Token、unknown和回滚，不能重复收费或继承H12付款许可。
