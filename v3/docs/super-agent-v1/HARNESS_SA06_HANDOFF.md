# SA06 固定本地工程交付（仅准备，未声称送达）

源码 15591aea214e7a83f3bf5ad112bda85b4294664e；源码SHA-256 2f47b76904cff3fb2e1c062258364e1a495a310150e4a759d42bfc16772ee100。
原注册验收 744PASS（本卡178+既有566），lint/typecheck229filesPASS，
28项交付hash匹配。证据：evidence/SAV1-06/20261111T082512Z-f4a54cb3/result.json。独立复验仍PENDING，SA07未启动。

0035_feishu_source.sql及sources/feishu*、专用表、/api/v3/feishu-sources和可选
/api/v3/webhooks/feishu归Codex，实际确认d0053f4 §29已只读核对。0036未预占，
未来Engine/API/预算重叠先另行协调。本轮未编辑/导入Harness活动树或推进H卡。

接口feishu-bot-v1，官方lark-oapi1.7.5来源Adapter+受控SDK包装，默认Runtime=None，
无自动启动/凭据加载。所有接受/失效/健康事实经原Engine同一PG连接；没有第二业务
状态机。来源身份是大小写敏感安装/租户/OpenID，角色/四层策略仍用SA02/03/04；
上下文输入不升格请求。独立Channel/Worker/模型/通知/发布未接通。

长连接成功ACK在持久提交后，原生协议+真实PG连接丢失、重建Engine和ACK丢失重投已验；
SDK专用跨进程SIGKILL和实际平台重推仍NOT_RUN。历史有界、当前重新鉴权、默认context_only，
无序版本sticky ambiguous，不猜编辑者/用户ID或无穷补齐；unsupported资源只保留无正文
失效事实。健康权限/话题不可见在fence后保留新鲜当前配置的负面原因，不能恢复旧许可。

当前Engine/ToolAuthority/预算/Harness和0001–0034原字节未改，旧GitHub Webhook AST不变。
来源Ingress仅内部current-role/kind context_only收紧；旧SA05升级断言仅追加0035，
原行与校验和断言保留。0035已在本副本PG安装，35份安装校验和一致。

本文件是本地交付准备，不等于新的消息已发送/送达/被确认。完整系统安全与全部真实
来源/编码/发布/长期门仍NOT_RUN；本轮真实API/付费/外部业务写0。完成后停在SA06，
等待独立复验，不自行开始SA07。
