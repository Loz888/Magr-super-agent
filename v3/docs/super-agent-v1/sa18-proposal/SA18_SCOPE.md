# SA18 精确审查与 Draft PR 共享范围

SA17-I01 f455c8e 已获用户接受独立 1 PASS，SA18 当前 IN_PROGRESS。本提案尚未确认，正式迁移、Engine/API 接线、整卡运行与独立验收 NOT_RUN；SA19 未启动。

仅在 SA 工作副本由 SA 实施：拟 0050 六张 Review 元数据/加密内容/展示/精确批准/PR-CI 关系/审计表；原 Engine 和 API 两文件的最小组合、draft_pr 验证分流、恢复与注册。完整 SQL、补丁、每文件 before/after 和 16 条路由在 proposal-manifest.json。没有新的 Action 类型，不改原 Dispatcher、预算算法、Harness 内核、旧迁移或 H12。

业务状态继续归原 Ticket/Run/Mandate/Action/Approval/Outbox，三个发布步骤复用原 repair_publication_steps。全部 SQL 经持锁 Engine 同一连接和 RLock；文件、网络及远端查询均在事务之外。默认没有 Profile、Worker、Dispatcher 或监听器；只有服务端显式注册的独立 fixture 读/写端口，HTTP 不接受 Provider、任意路径、凭据、Diff、成功声明或 actor。

固定 H08：Harness 提交 a09aef760e4c43ff46595a3c3cc56c503b018e97 中的 H08 记录，实施 fadea5d93d0b6a54bf855eb24eed4a382e6e3125，独立复验通过。该卡提供增量编辑、直接前像 hash、完整结果/展示约定，不是审批授权。其 service.py 文件任务库与后续 H12 github_delivery.py 不导入；当前 SA13 已固定的 H03 工作区、受控 Coding/Verifier 及原 ReviewBundle 足以组合本卡受控工程范围。H08 的历史受控付费记录不转移任何额度或实时权限。

审查由 Owner cookie 与现有 Origin/CSRF 完成。准备入口只接收 Coding/Verification ID、epoch/input revision 和已登记 profile hash。服务端重读当前 Source、身份、角色、策略、Mandate、原任务和最新测试事实，读取实际 H03 工作区完整基线/已测树；既有 UTF-8 普通文件、100644、全树 ≤1 MiB、单文件 ≤256 KiB、最多 16 个变化文件。新增/删除、二进制、软/硬链和不能完整展示的变化失败关闭。首片不放宽 SA13 的可写/测试边界。

封装原 ReviewBundle、完整 Diff、变化文件内容、原测试回执引用和实际树 hash。固定 repository/base/branch、目标 Ticket、原 input/epoch/async revision、来源和 Owner；发布正文只包含最小结果/任务引用，不复制群聊、任务原文或日志。完整代码/Diff 仅加密短期保存，最长一小时且不超过原许可；来源撤权/编辑删除/策略身份变化在原事务失效并擦除。已发/未知动作及无正文回执事实保留，不能以取消抹除远端结果。

POST inspect 返回完整有界 Bundle 并记同 Owner/同会话/同 hash 的短时展示回执（≤120 秒），不声称证明人已阅读。批准须确切 presentation_id/bundle hash/action hash、当前权限和单独写许可，然后在原事务创建原 Approval/Outbox。IM 文本、NotificationGrant、模型输出和旧通用 approve 不替代该回执。未配写端口仍可审查/导出，停留待审批；过期须重新核验并形成新提案，不自动续签或刷新。

发布复用原 Workbench 三步骤传输协议，由 SA 私有适配器接入原 Action 和 repair_publication_steps。每次实际远端写之前，在步骤预占提交后重新检查当前 Source/Owner/审批/目标/epoch/async revision/实际已测树、原 primary/目标写 fencing、许可有效期与远端基线。远端完整 tree、全部 blob、唯一父 base、Draft 状态/标题/正文/仓库/head/base 必须一致。新增复核补齐旧 Workbench 只比变化 blob 的不足。

已接收而丢回执保留 outcome_unknown，只通过原 Engine 只读对账；缺 branch/commit/PR 或对象不符不继续写。最小 hash 元数据支持正文过期后继续核对真实副作用。PR 和获准 SA05 原生 CI/head/attempt 事实只能绑定原 Ticket，force-push 不继承旧验证，跨仓/安装/私密来源失败关闭。原 SA17 review_available 通知继续仅提示可审查，原话题获准通知不会替代批准或推进发布。

独立页面 /source-reviews 与两个专属静态文件归 SA，不修改 web/agent.*。页面沿用 Owner 认证，严格 no-store/CSP、安全文本渲染，完整显示目标、基线、实际树、测试、Diff、内容和期限。fixture:// 仅显示受控回执，不当真实 PR；缺批准条件显示具体等待原因。SA19 控制台总览不提前实施。

真实测试仓库、平台、付费模型、外发、发布和长期运行继续 NOT_RUN；本副本 H12 真实 Agent/MVP 既有 FAIL 保留，另一 CLI 负责 H12 验收。SA18 的受控工程不能把这些门改为 PASS，不提供 merge 或部署。主库仍 44 份迁移，0048/0049 及拟 0050 不安装主库；0051 以后不预占。
