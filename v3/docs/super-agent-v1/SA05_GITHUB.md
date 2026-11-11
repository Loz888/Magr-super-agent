# SAV1-05：GitHub App 与事件源适配

实现中；整卡结果以 accept/SAV1-05/accept.json 为准。契约 github-app-v1；不启动真实 GitHub App、监听、模型、Worker、通知或发布。

## 设置与激活

先注册 SourceInstance：github、tenant_id=GitHub 账户不可变 ID、installation_id=安装 ID、adapter_version=github-app-v1，credential_ref 仅含 vault 引用。当前首版单项目、单安装、单仓库。GitHubSources.configure 只保存非密钥设置：App/account/installation/repository ID、仓库名称和可见性、事件、human sender IDs、context bot IDs 与 own bot IDs。own bot 永远不能加入输入白名单；机器人只可通过已有身份系统绑定 context_provider。

每次保存使用 expected_revision CAS、review_ref 与当前控制台主人 cookie；并追加 SourceInstance 版本。随后须重新核对角色、签署相应版本策略并配置 SA04 owner/generation，旧策略、正文、grant 和 Runtime 不自动升级。所有管理路径要求原 Origin/CSRF，响应 no-store。配置/API 不接受平台 secret 或 private key。

GET/PUT /api/v3/github-sources/{source_id} 查看/保存；PUT /{source_id}/snapshot-grants/{grant_id} 创建明确只读快照授权；POST /{source_id}/snapshot-grants/{grant_id}/revoke 撤销。不存在公开 mint-token、原文快照或身份自证端点。完整来源设置 UI 留待 SA19；当前通过已认证管理 API。

受信服务端显式注入 GitHubSecrets、GitHubActivation 与 GitHubRuntime；默认 create_app 的 github_runtime=None。activation 绑定 source/config revision/repository ID、操作集合、UTC 到期和 review_ref。controlled 模式强制 MockTransport，无法访问真实网络；live 模式仍需要另行经过实际资源/数据范围审查。没有自动载入真实私钥、启用脚本或常驻进程，本轮不生成真实 activation。

## 验签、身份与资源

复用原 SHA256 HMAC/constant-time 验签；拒绝重复认证 header、重复 JSON key、错误安装/账户/仓库/可见性、未选择事件/动作及 sender ID/type。实际 sender.id/type 决定来源 actor，保持原登录名的案例不会改变 numeric actor ID。正文、显示名、quoted user、评论中的管理员文字不产生权限。

首次当前签名事件可记录最小身份 evidence；绑定角色仍需主人审阅。SA02 严格 proof replay API 保持不变；Runtime 使用原 Engine mutex 串行化“已有 evidence 检查/首次观察/SA04 入站”。崩溃后重投使用真实持久事实，不缓存授权或产生第二写者。

规范化八类：issues、issue_comment（包括 PR 会话评论）、pull_request、pull_request_review、pull_request_review_comment、workflow_run、check_run、check_suite。SourceThreadRef 的 conversation=repository:<immutable-id>，已明确资源使用 issue:<n>/pr:<n> 原生话题；无 PR CI 不伪造 issue_number，多 PR CI 保留完整有界列表、不猜第一个。

加密 github-content-v1 正文包含经过校验的 GitHubResource 和原文/有界 CI 状态，保留仓库 ID/名称/可见性、PR、head/base SHA、head 仓库、run/attempt、check/run/suite/producer App ID 与原作者。SourceEvent repository_hint 只是事实提示，不能提供快照或外发许可。

GitHub Webhook 没有可验证的全局连续序号或签名时间。发生时间取平台资源时间；没有原生单调编辑序号，derived hash 包括实际状态/内容/资源时间，native_order=None。未知不同编辑继续触发 SA04 sticky ambiguity，等价事件/版本重投复用 canonical。不能声称通过 updated_at 排序或自动解除歧义。删除 tombstone 和当前撤权约束沿用 SA04。

## 唯一入口与可靠性

旧 /api/v3/webhooks/github 在 workflow/Issue 等分支之前查显式 owner。gateway 只进入当前同一 Engine 的 Runtime；无 Runtime、disabled 或旧 generation 失败关闭。legacy 只走旧入口；回切后已有新 delivery 仍被阻止进入旧 proactivity/ingest。未配置仓库保持原入口兼容。

ACK 即成功 HTTP 响应，在 SourceGateway 返回 durable committed receipt 后才生成；数据库或验证失败不产生成功 ACK。继续继承 SA04 的同一 PG 持锁连接、最外层 synchronous_commit、冲突、容量、撤权与加密正文。没有平台 SDK 自动 ACK，ASGI 受控测试与 SA04 真实 PG/进程故障回归不替代实际 GitHub 平台投递可靠性。

## 只读凭据、快照与历史

Broker 使用 RS256 App JWT（iat 含时钟偏差、exp 小于10分钟），POST 安装 access_tokens 仅为精确一个 repository_ids 和 read permissions。返回必须证明 scope/expiry/permissions，令牌保留在 Broker 调用栈；无持久 token cache、日志、URL token 或项目进程凭据。仅固定 api.github.com、官方 API version、超时/字节界限；禁重定向和环境代理，不跟随 payload/download_url。

快照须另有签署 grant：当前配置/owner generation、identity revision、context_provider+requester、当前启用 ExecutionGrant/read_repository、精确仓库/base SHA/路径和有效期。逐文件限制与 Git blob SHA1/文件类型/路径/大小核验，文件内容只在所有检查通过后返回受信调用者。已授权 PR 还核对当前 base/head 仓库和 SHA，fork 或 force-push 不继承许可。它是 SA13 受信快照通道的工程端口，本轮不将私有内容接入 Worker/模型，也不声称当前 Harness 私有仓库执行门已通过。

GitHubHistory 只补拉经当前 Receive 许可的指定 issue/PR 会话评论快照，最多100条/页与100页显式页码。没有任意 URL、无限循环或历史自动任务。所有结果由原 Gateway 再鉴权并 context_only 提交；删除/编辑完整历史与 CI/webhook delivery replay 不可由评论 API 补齐，缺口保持可见。补拉不是无损消息日志，完整真实 API scope、删除可见性、平台保留和权限变化须实际安装验收。

401/403/404、rate limit、丢响应、过大/矛盾数据分别给固定原因，不记录远端正文或凭据，不自动重试。限流等待真实 Retry-After/reset，后续明确操作仍重查当前权限；不伪造已读取、已补齐或已发送。

## 复现与限制

根目录 make lint、make typecheck、make verify TASK=SAV1-05。注册采用真实隔离 PG、真实 HMAC/RS256、合成录制与 httpx.MockTransport；包含 SA04 及旧入口回归。保留全部失败和 NOT_RUN；新迁移不得修改 0001–0033。

平台安装、凭据分发、真实 API/监听、私有源码外发、模型、编码、通知、发布及长期门未执行。Settings/health 中 unknown 与 disabled 不表示平台权限已验证。完成工程后停在 SA05 等独立复验，不进入 SA06。

## 追加边界说明

可信编辑/删除即使来自未选入 App Receive 白名单的 sender，也须使已经保存的旧正文失效。Runtime 检查策略 actor 集合不超过 App 的 ID/type 上界；此类变更交给 SA04 作拒收/最小水位或 tombstone 处理，不建立该 sender 的新身份 evidence、不保留新正文。创建事件、自身输出和未知 actor type 仍在入站前拒绝。

短期凭据签发前、签发后且真正数据 GET 前、GET 后释放前，快照/历史都重查各自当前授权。撤权发生在令牌签发期间时不继续数据 GET；已发 GET 的数据在当前授权失效时不释放。外部请求在事务外，不能撤回已经到达平台的读取。

GitHubRuntime.probe_permissions 是显式服务端只读工程端口，按已选事件申请必要 read 权限并读取单仓库权限证明；管理 GET 仅返回最近五分钟的已观察状态，不触发网络。受控探测标 mode=controlled，真实渠道门仍 NOT_RUN；配置变更/activation 到期使旧健康状态回到 unknown。没有探测时 permissions=unknown；连接状态不替代事件/快照/模型/外发许可。

旧 SA04 32→当前升级测试的期望迁移列表改为完整追加清单，仍严格检查原32次迁移、旧业务行与全部安装 SHA-256；仅为新增0034的机械更新，不改其故障/隔离断言。

## Git 路径证明

Contents API 会跟随某些仓库内符号链接，因此快照不使用 Contents/download_url 获取源码。Broker 从获准 commit 取得 root tree，按有界非递归 tree 逐级核对每个路径分量，仅接受普通目录及100644/100755 blob；120000符号链接、160000子模块、非文件、截断/矛盾 tree 均拒绝，再按精确 blob SHA 读取和核算 Git 对象哈希。最多100个文件、16级路径、128个tree对象，单响应2MiB且总文件字节受 grant 限额。Git 元数据只留在受信 Broker，不把目录树或凭据交给 Worker。

工程回归使用 git hash-object 独立核算对象字节，受控 blob 返回 API 风格多行 base64；不以 Contents 返回 type=file 推断路径不是符号链接。
