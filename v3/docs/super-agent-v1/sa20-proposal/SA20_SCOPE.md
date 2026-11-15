# SA20 常驻服务范围提案

本提案未确认，0053草案和共享精确补丁均未应用；主库44迁移不动。正式范围需Harness新确认。

Engine继续持有唯一PG写锁与连接。API/Engine负责原业务提交；独立Source relay、Scheduler、Worker、Dispatcher和Watchdog进程只有固定本机HTTP接口与各自私有凭据，没有PG、模型或平台宿主凭据。业务执行复用原SA12/14/17端口与原授权，不引入第二套Thread/Ticket/Run或财务状态。Source relay只转交平台已签名的原始Webhook；原Runtime重新验签/权限并耐久提交后才能ACK。长连接与真实平台能力仍需独立实测，不用进程心跳冒充平台连接健康。

0053四表仅保存Owner签署的运维配置版本/当前引用、运维组件租约与待确认tick、无正文告警事实/明确wait或escalate决定。角色开关默认关闭，配置有到期及CAS；Source角色精确绑定Source ID。平台/Model/Run/Scheduler/Notification/Publication许可继续各自校验。运营授权不创造付款、工具或发布权。组件持久generation/boot/heartbeat sequence阻止旧进程；tick sequence/id和reserved/unknown跨重启保留，不能通过重注册清空未确认工作。

独立Watchdog只读取限定元数据快照，提交程序重算的告警建议。快照区分服务在线、真实来源权限、gap/积压、无Runner等待、Job/Assignment、原语义进展与未知费用/投递。心跳不推进原Run进展游标。建议按事实cursor去重，不使用当前采样时间制造告警风暴；unknown只读核对，不自动重发或重置预算。Owner仅能对新运维告警wait/escalate，原业务控制继续原API。

三文件精确补丁：Engine只组合SourceOperations；主API只注册11接口，并将既有Host/Origin集合参数化为一个严格HTTPS origin；Settings只增加web_public_origin及服务端operations_service_config路径与对应纯验证/属性。默认回环集合保持，公网模式强制Secure cookie，HTTP首次Owner setup关闭，需先本地准备。无通配Origin/Host，不依赖任意X-Forwarded头授予身份；运维服务接口限回环客户端与分组件令牌。旧API业务方法、预算、ToolAuthority与H12不改。

SA私有范围：source_operations*.py、tests/sa20_*及test_source_operations_*、deploy/super-agent-v1/*、schema/fixtures/config专属文件与本提案。部署优先提供隔离的systemd/Compose与TLS反代，PG/Redis无公网发布，角色环境经清理、不挂载.env/PG凭据，默认无平台/模型/通知/发布激活。没有真实部署或72h运行，不标真实门通过。

备份明确绑定真实PG dump、CAS、Source正文密钥和控制台凭据密钥。文件操作/pg_dump在业务事务外；只允许已静默/离线的授权快照，不假装任意运行中多介质复制具有一致性。加密包校验清单、相对路径/大小/符号链接与项目/迁移绑定；恢复仅进入新隔离库/目录，默认不激活，保留unknown账本、游标和撤权事实。不会用回滚DB撤销远端写。

准备检查只验证新私有契约/纯信号及0053隔离草案，不替代正式接线。正式范围还须检验限权/撤权、API/TLS/CSRF、真实独立进程、停止浏览器后原任务继续、Runner丢失/服务重启、来源/通知故障和DB/CAS/密钥恢复。已通过项目不重跑，失败记录与后续版本均保留；SA21未启动。
