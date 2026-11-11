# ADR SAV1-06：飞书机器人身份、SDK 持久 ACK 与保守历史

状态：IMPLEMENTING，feishu-bot-v1；继承 SAV1-00/02/03/04/05。只授予本地受控工程，
本 ADR 不配置凭据、不启动真实来源、模型或外发；SA07 仍 PLANNED。

官方 lark-oapi==1.7.5 的原生 Frame、dispatcher 和响应序列化用于长连接。受信回调
必须先经唯一 Engine 的持锁 PG 连接提交，SDK 才能返回成功 ACK。包装器限制分片、
并发、时效并隔离 session epoch；不依赖 SDK 无限历史重推，不用提前 ACK 的内存队列。
Webhook 是默认禁用的备选入口，强制签名、token、时间、安装/租户核验及有界流读取；
客户端填写 socket proof 头不能替代私有当前连接证明。SDK 普通日志不能记录正文。

安装/租户/App/bot/open_id 均保持原值及大小写，显示名、正文中的 @ 和模型不能绑定
身份。群、发送者、结构化 mention、事件及精确 followup topic 独立选择；此显式接收
话题不是提前实现 SA11 动态业务订阅。未选群/私聊只留有界最小传输 hash，不能保留
正文或产生业务输入。已受信的选中群内资源变更即使被发送者、@ 或类型过滤，也在
同一原事务对已知实体提交最小无正文失效；不能让旧正文复活。冲突先检查、回滚原子。
来源接收允许不隐含请求、控制、执行或通知许可；非人类/仅上下文角色始终 context_only，
由原 sources/ingress 内部 grant 判定，不伪造 historical_backfill。

历史采用固定群 API、分页/字节/页数上界，每次 IO 前后重验当前身份、策略和配置；
所有历史经原 SourceGateway.history，保持 context_only。快照只证明资源当前内容作者，
不伪造最后编辑人；unsupported 类型只提交真实类型/hash 的无正文变化事实。平台没有
可靠原生单调修订序号，因此 derived 版本差异沿用 sticky ambiguity，不使用 update_time
或收到时间猜排序；历史受限或不能回放时留下 gap，不宣称补齐。

server-only Secrets 与受限 Broker 不交给浏览器、来源正文或 Worker。internal tenant
token 继承 App 实际平台权限，不冒称平台签发了只读衰减 token；Broker 固定只读端点、
无重定向/代理、无缓存，并在事务外 IO、释放结果前重新鉴权。权限/令牌失败 fence
旧 Source revision，保留真实 gap，管理员必须重确认，不能由重连自动恢复旧许可。

0035、新 sources/feishu*、专用表及默认禁用 API 的唯一所有权由用户实际确认，
并核对 Harness 固定 d0053f4d7bdf850f4c3c458bd36d3c10b9a3e650 §29；不导入/修改
Harness 活动树。不改 Engine 类、ToolAuthority、预算或历史迁移0001–0034，不注册
第二业务状态机。旧 GitHub Webhook 分支保持；旧升级断言只机械追加新前向迁移期望。

本地真实 PG 与官方 SDK 受控协议验证只构成 engineering_gate；完整系统安全、真实
飞书/GitHub/编码/发布及长期运行仍 NOT_RUN。失败与未执行逐次留存，固定候选完成后
等待独立复验，不能自行推进 SA07。具体能力、权限和引导见 ../SA06_FEISHU.md。
