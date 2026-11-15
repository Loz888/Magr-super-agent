# SA20 本地受控工程交付

SA20已完成本地工程交付，状态IN_PROGRESS、等待独立验收。源码候选`79188c23a9e035a27871dfe32842f1c55f85e430`，官方源码SHA256 `de08985496c78ae84a06913c3354f6685b35aece9690fcbc9b981e7d6d1a6510`。SA21未启动；真实平台/付费/外发/发布、全系统安全与长期运行均为NOT_RUN。固定H12真实Agent/MVP FAIL保留，由另一CLI负责。

本轮核对Harness固定提交`1c4f494442d2d5fc8d4c1d35856cc91c922cc70e`的第46节及七份完整提案哈希，仅读取其提交对象。正式0053四表、原Engine/API/Settings三文件和11接口按确认落地；随后对新增API import作唯一排序修正，原精确补丁与[样式增量](sa20-proposal/shared-wiring-style-delta.json)均保留。没有应用H12或占用0054。

交付包含[常驻控制与权限说明](SA20_OPERATIONS.md)、[systemd/Compose/TLS/Secret/故障/备份恢复手册](../../deploy/super-agent-v1/README.md)、原服务的限定HTTP端口、Source签名relay、Watchdog元数据快照/告警及加密DB/CAS/三种密钥恢复。业务与财务状态仍由原Engine持锁会话提交，项目命令在原隔离Provider中执行。

| 正式范围 | 分版本PASS | 主要源码 |
|---|---:|---|
| 原Engine/0053/Owner与组件API/撤权/unknown/HTTPS | 24 | e87b82a的11；e879c06的11；acfbc29的2 |
| 实际PG dump/restore、CAS/密钥、TLS网关及服务配置 | 18 | 7b70345 |
| 真实独立角色/两个SIGKILL/原队列/唯一控制进程失联 | 4 | e2f0fe5 |
| 原SA14异步Job/SA17 unknown/签名来源/凭据撤销/真实Redis失联 | 5 | e2f0fe5 |
| 当前组件历史有界投影、Watchdog boot/generation | 3 | 72f4e66 |
| 原ConsoleVault使用规范恢复路径、持久hold与unknown | 1 | d69dfb2 |

正式55个独特用例均有PASS；此前70契约/隔离草案与18客户端检查的88项历史PASS保留。累计143个独特用例按其源码版本报告，最后候选未重复运行已通过的55/143项。正式完整注册入口为`make verify TASK=SAV1-20`；阶段入口、精确命令/退出码/JUnit/归档及逐例历史见[机器摘要](accept/SAV1-20/accept.json)。

原正式首次为11PASS/13FAIL，定向复验13→11PASS/2FAIL，再仅2项2PASS；其余范围均是首次新检查。失败来自闭包API注解解析、严格计数器的bool、两个正向HTTP夹具默认peer与R1误用pause。对应原失败日志完整保留。后续静态错误只复验失败目标；无已通过功能用例重跑。

测试证明的具体结果：

- 公网模式拒绝首个Owner setup；实际TLS登录cookie含Secure，Host/Origin/CSRF生效，内部组件/Watchdog/MCP/Worker路由由网关拒绝。
- 组件按项目、角色、当前配置、到期和boot/generation/sequence fencing；旧nonce重用已知回执，部分执行或SIGKILL后tick保持unknown，不因重注册重发。最后一步组件凭据撤销阻止实际派发。
- 原队列跨Controller SIGKILL保留；无配置Runner时等待，原Worker控制进程失联后liveness到期。HTTP客户端关闭后，受控原SA14工作实际修改Git工作区并完成Docker测试，结果仍处于原审查/业务状态，发送不等于业务成功。
- 来源签名在原Runtime校验并durable commit后才ACK；错误scope/签名不成功。受控Runtime断线事实与gap可见，未将进程心跳记为平台权限。SA17丢回执只留unknown，一次发送后不重发；实际Redis停止后PG原队列仍在。
- 备份/恢复使用真实PG16工具、加密包、真实CAS与独立密钥；篡改/缺key/链接/错误项目/迁移/非空目标被拒绝。恢复后的原ConsoleVault能读取规范路径，旧unknown Action保留，默认启动被hold阻止。最新撤权/删除和远端写尚需人工核对，工具不自动激活。

这里的模型/平台服务是明确受控fixture，实际Git/PG/Docker/TLS/本机HTTP/进程是真的。Worker控制进程与实际项目容器分开，受信编排仍在唯一Controller；不称真实模型Agent已经迁移到远程执行宿主。未测试用户物理电脑断电、真实在线服务器整机/systemd启动、正式证书链、真实长连接或72小时运行。

静态检查按各自源码范围记录：原全量318文件typecheck和原lint修复结果保留；新增控制、客户端、恢复、原生组合、共享接线、恢复路径及离线盘点均各有其定向检查证据。最终完整lint/typecheck/功能全量没有重跑，不能称为最后源码单轮全仓通过。

每轮先提交、Git归档、重算官方源码指纹，再执行已冻结检查；额外SQL/补丁/确认输入也与归档一致，检查前后源码和输入均稳定。正式仓库50份迁移、19份Source目录、654个产物引用、17份运维schema；主库55435仍44份迁移，没有运维表。最终候选、静态收尾、机械盘点和清理结果见机器摘要的closeout记录。

`.env`、用户未跟踪文件、SA19独立证据与历史失败保留。隔离PG/数据库会话、测试监听器、角色进程及本卡临时Redis/故障容器均按其所属范围清理，原H06与其他CLI资源保留。没有在用户宿主安装服务或启动真实能力。

最终盘点在3fd5ac4通过；79188c2只修正新盘点模块的类型注册注解及相应目录SHA，剩余单文件mypy目标PASS。已通过的盘点、lint和143功能用例均未重跑。最后受测候选已归档，源码指纹前后一致；18份受保护文件哈希一致，全部本卡隔离PG已删除、角色进程/本卡容器无残留，原H06存在。外层汇总器一次命令全文归并错误另存，改为核对后续通过的同等/包含目标集，未重跑测试。详见机器摘要closeout。
