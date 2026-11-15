# SA20 受控常驻部署与恢复

本目录交付新部署配置和本地受控演练所用的运行路径。固定Harness H12真实Agent/MVP仍为FAIL；真实来源、模型付费、通知/发布和72小时运行均未验收。不要将本目录当作已部署到用户服务器的证明。SA20正式结果见 [工程交付](../../docs/super-agent-v1/SA20_ACCEPTANCE_20261115.md)。

部署单元与权限：

| 单元 | 启动入口 | 持有的权限 |
|---|---|---|
| API/Engine | `source_operations_runtime:create_application` | 唯一PG持锁会话、CAS及当前原业务服务；后台旧supervisor关闭 |
| Source relay | `python -m mgar_v3.super_agent.source_operations_relay --config ...` | 本机限定Source组件token及精确Source ID；转交原始签名事件 |
| Scheduler | `source_operations_client`，role=scheduler | 请求原调度维护/已签署自主规则cycle；不创建新付款许可 |
| Worker控制进程 | 同上，role=worker | 请求已装配的原SA14 Runtime；项目命令仍在原H03/H06隔离容器 |
| Dispatcher | 同上，role=dispatcher | 请求原SA17 Action/Outbox派发；未知消息只读核对 |
| Watchdog | 同上，role=watchdog | 无正文快照与程序重算告警；Owner只作wait/escalate审计决定 |
| HTTPS gateway | `source_operations_gateway --config ...` | TLS私钥与两个固定回环上游，无业务/组件/平台凭据 |
| PG/Redis | 本目录Compose | 独立私网；主机仅127.0.0.1端口15432/16379 |

这些控制进程通过HTTP调用原Engine端口；受信授权、记账和部分Harness编排仍在唯一Controller内，实际项目工具进程由既有隔离Provider承载。此交付不声称已经把真实模型Agent迁移到独立远程主机。默认未装配Worker/平台Runtime，任务明确等待Runner；Source组件心跳也不代表平台连接已验证。

安装与初次引导：

1. 固定交付源码和Python3.12锁定环境，安装Linux的PG16客户端工具（`pg_dump`/`pg_restore`）以及Docker。首次实例使用新PG卷、新CAS和新密钥；部署路径建议`/opt/mgar-sa20/v3`，状态在`/var/lib/mgar-sa20/controller`。
2. 创建Controller、Source、Scheduler、Worker、Dispatcher、Watchdog、HTTPS各自的系统账户。代码只读；状态目录仅Controller可写。为每个角色创建其独占0700配置目录，私有JSON必须属于该进程账户、模式0600、普通文件且无符号/硬链接。Controller的组件token文件也须属于Controller。systemd读取的`server.env`可由root以0600管理。
3. 复制示例到私有目录，替换每个UUID/token/项目/仓库/密码/TLS证书。每个组件使用独立随机token（至少32字符），HTTPS只配置TLS与固定上游，角色进程没有`.env`、PG或平台/模型凭据。禁止将真实值提交到Git或聊天。
4. 在目标实例运行本目录Compose，显式指定自己的项目名和配置。样例保留`127.0.0.1`端口绑定；API与Source relay也只监听回环。主机入站仅开放经过TLS的HTTPS端口18443，或由受信L4入口转发443。禁止将18080/18082/PG/Redis直接公开。
5. 空实例运行正式迁移。先用`WEB_PUBLIC_ORIGIN`未配置的本机API或可信SSH隧道创建首个Owner。之后配置一个精确HTTPS origin，安装可信证书，再启动HTTPS。公网模式会拒绝首个Owner setup；cookie强制Secure，Origin/CSRF与已有账户一致。
6. 默认业务付费与外发为0，Source/策略/通知及运维角色均分别签署。Owner通过`PUT /api/v3/source-operations`保存enabled、有界到期、components及review_ref；CAS使用当前revision。保存运维配置不会安装/启用平台或提供Worker/模型权限。
7. 管理员审阅后安装本目录的systemd文件，使用系统级`multi-user.target`服务。它们不依赖桌面登录。模板不会自动安装、迁移或启动现有用户服务；本卡没有在当前宿主执行安装。

`create_application`显式禁用`.env`自动加载，配置来自Controller的服务环境。`--workers 1 --no-proxy-headers`及唯一Engine锁共同防止第二个写者。已存在同库Controller时新进程应失败，不能删除锁来启动。

原Runtime装配：

默认入口提供原Scheduler与SA17 Dispatcher的确定性端口，Worker保持不可用。显式受信服务端组合可将已经注册并通过对应能力门的`SourceAsyncRuntime`、现有Scheduler session与精确bearer装配为`OriginalAsyncWorkerPort`，再调用`engine.source_operations.bind(port)`。它仅启动已有有效SA14 binding、原Assignment和Coding Mandate，不新建业务许可。平台Runtime同样通过原`create_app(..., run_background=False, feishu_runtime=..., github_runtime=...)`参数注入。使用provided Engine的组合须由调用方管理恢复/关闭与Provider生命周期。当前实测只覆盖fixture_git、受控模型、真实本地Git/PG/Docker；不要据此启用真实自动修复。

HTTPS与Source回调：

公开回调URL为`https://<固定域名>:18443/callbacks/<source_id>`。HTTPS将该路径交给固定18082 relay；relay携带本机组件token和当前boot/generation转交Engine。只有原Runtime重新验签、当前来源权限检查并持久提交后，relay才返回成功。上游失联、拒收、scope不符或不确定时返回非成功，等待平台已声明能力的重投。重复由原Ingress去重。relay不伪造socket证明、不绕过SDK ACK限制，也不提供无限历史或本地额外业务队列。

HTTPS屏蔽组件、旧Watchdog、MCP及Worker内部接口，并移除内部鉴权/Forwarded头；上游Host来自固定配置。HTTPS证书须覆盖配置域名/IP，TLS私钥为0600。更换证书后重启gateway；维护证书到期与续期流程。真实飞书长连接、平台权限和实际公网证书链未在本卡验收。

运维观察与恢复：

- Owner用`GET /api/v3/source-operations/metrics`查看组件租约、来源连接/权限/gap、积压、语义进度、Job、Assignment和unknown；`/incidents`给出是否仍受当前事实支持。最多16当前组件、32来源及32 Run，超限显示partial；列表不是完整历史导出。
- Watchdog每轮重新取快照和事实cursor，按当前scope/boot/generation提交。心跳不推进原Run进度；Job的overdue使用原Mandate时限/最大命令授权上界，不把普通采样时间或心跳当进展。必要命令的更短timeout仍由原执行Provider处理。
- 停止浏览器或HTTP客户端不会取消云端Job。唯一可用Worker控制进程失联后，租约到期显示等待Runner；真实物理Runner/云故障还须独立验收。当前受控工作由在线Controller与原隔离Provider承载。
- Controller重启由原Engine恢复业务状态，再把已reserved运维tick标为unknown。unknown不能通过重开组件/换boot/新nonce清除；只读核对原业务Job/Usage/Outbox，不能重发、重置预算或伪记成功。本版没有unknown强制清除接口。
- Source offline、权限不足与gap分别观察；来源组件存活不抹掉平台故障。Redis探测仅作可重建依赖健康，PG队列仍是权威。PG故障时不ACK未持久输入、不生成“已保存”的告警。
- SA17投递失联仍保留原Action/Outbox unknown；ops tick的known只表示一次有界调度已返回，不等于消息已发送。没有可靠查询能力时等待人工。
- 停用运维配置阻止后续组件操作。停止业务任务、撤销来源/策略、处理正在执行的原子命令继续通过原控制台/SA14协议；运维角色开关不替代这些业务操作。

备份与恢复：

仅支持显式停服务、停Job后的离线一致性备份。先停止新接收/领取，核对Job与在途外部写，再停止角色和Controller，保留来源重放/gap边界。数据库锁预检只证明无有效Engine/迁移进程，操作者仍须确认外部项目进程已经停止。

私有配置见`backup.example.json`：真实PG dump、CAS及三种已实际使用的Source正文/SourceConsole/旧Web凭据密钥一起加密，另用独立32字节随机备份key；备份key留在包外的安全位置。不存在或无效的密钥、非规范CAS、链接、超限、同名输出及仍活跃Engine均拒绝。受控包上限128MiB，实际容量需另测；不支持任意在线多介质快照。

```bash
/opt/mgar-sa20/v3/.venv/bin/python -m mgar_v3.super_agent.source_operations_backup backup --config /etc/mgar-sa20/backup/config.json --output /srv/mgar-backups/UNIQUE.encrypted
/opt/mgar-sa20/v3/.venv/bin/python -m mgar_v3.super_agent.source_operations_backup restore --config /etc/mgar-sa20/backup/restore.json
```

恢复只能选择新空库和新私有目录，绑定项目与完整迁移集合SHA。PG/CAS/密钥哈希匹配后保留`restore-hold.json`；SourceConsole/旧Web消费者的规范路径同时恢复，Source key在`keys/source.key`供受信组合选择。备份中的旧scope、unknown费用/投递、游标和租约事实都保留。默认服务入口在启动Engine前拒绝带hold的目录。

在隔离、断网副本核对并重放备份之后的撤权/删除记录，核对旧远端写与费用、Job和lease，轮换会话与服务token，恢复原Runtime的精确配置及密钥路径，再决定是否允许重新激活。没有新鲜撤权/删除账本时保持hold，不从旧备份自动发送pending。只有操作者完成这些步骤才可移除hold并重启；工具本身不授予发布或恢复授权。

检查与证据：

`make verify TASK=SAV1-20`注册正式55个独特用例；本轮按用户要求只首次运行新增范围，修复只复验失败用例/目标。已通过的准备70/客户端18与正式成绩按各自源码版本保留；不能称最后候选一次全量143PASS。旧准备overlay入口只用于其固定源码归档，正式验收不重复安装草案。

可运行离线盘点`python -m mgar_v3.super_agent.source_operations_audit`，核对50份正式迁移、19份Source目录、schema、全部产物引用与共享确认。加`--main-local`仅用于本工作副本原55435主库的只读核对，不运行SQL迁移。该盘点是机械证据，不替代独立验收或真实72小时运行。
