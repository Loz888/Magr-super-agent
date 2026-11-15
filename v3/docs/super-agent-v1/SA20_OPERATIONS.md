# SA20 常驻部署与运维：当前实施边界

状态 IN_PROGRESS。SA19 045934c/40bbbcf的I01独立3PASS已核对并获用户接受，见[独立关闭](accept/SAV1-19/independent-closure-20261115.json)。当前只完成SA20阶段准备和私有限权客户端，**尚未完成SA20正式接线、部署或独立验收**；SA21未启动，真实能力门NOT_RUN，固定H12 FAIL保留。

共享提案a9e2a7a／准备记录3f75b60包含0053四张运维事实表、原Engine/API/Settings三文件精确补丁、11个接口：[转交协调请求](sa20-proposal/ownership-request.txt)、[完整边界](sa20-proposal/SA20_SCOPE.md)、[精确哈希](sa20-proposal/proposal-manifest.json)。SQL SHA256 `1ae8bd6ee189f8efe7f937a2ccc90e2b49b2012107f751426b9d8a27e10d2c37`，补丁 SHA256 `ecc71e2f4effe7ccf138fe1b2663e33b22481e2e065828c41dcc28626a86c74a`。

正式0053、三文件接线均未应用，当前共享文件仍匹配提案的变更前SHA。尚待Harness单独确认提交与HANDOFF节号；第45节只覆盖SA19。AGENTS.md要求共享Engine/API/schema/budget变更前约定所有权、接口和迁移编号，本提案不导入H12，不预占0054。

已实现与验证的阶段范围：

- 70项契约、纯观察信号和0053真实隔离PG草案检查有分版本PASS。首轮a9e2a7a为67PASS/3夹具ERROR；原因是同一临时session库重复安装草案。78513a4仅原3错误复验3PASS，原67未重跑；所有测试函数AST/断言保持，SQL草案未改。原Engine未组合SourceOperations，正式迁移账本仍49，草案只在临时库额外创建4表。
- 角色和Source范围、到期/CAS/boot/generation/sequence、未知tick保留、严格HTTPS origin、本机服务凭据、备份清单中的DB/CAS/密钥绑定，以及heartbeat与语义进展区分均有契约/纯信号检查。此类通过不等于真实运行、TLS反代或实际备份恢复通过。
- 新私有 `source_operations_client.py` 的18项首次检查在f3e260f通过：真实文件权限/符号链接/硬链接/FIFO/大小/重复字段边界，以及分角色HTTP流程、禁用时仅观察、unknown只读核对和进程对象重建后不重发、拒绝重定向与无界响应。HTTP端使用明确合成MockTransport，不把它称为真实常驻进程或平台成绩。
- 客户端只从明确选择的私有0600常规文件读取配置，不加载Settings/.env/PG/平台凭据。只向固定本机Engine的限定路径请求，禁用环境代理和重定向，不接受任意命令/URL。Source heartbeat不声明平台连接成功，Watchdog只提议元数据告警，unknown tick不得被重注册清空。原业务许可和费用账本仍由正式接线后的原服务逐操作验证。

静态检查按版本记录：初全量typecheck318文件PASS不重跑；初lint8处SQL长字符串修复后仅失败目标PASS。新客户端lint的UP047在32b9f4b修复PASS；HTTP kwargs类型错误原始失败保留，一次文本替换未命中实际格式化调用，随后精确改为等价的显式json参数。1670ba9仅剩余单文件mypy失败目标PASS；18项和70项通过均未重跑，不宣称最后源码单轮88项或全仓静态通过。

[70项准备摘要](accept/SAV1-20/preparation-20261115.json)和[18项客户端及阶段摘要](accept/SAV1-20/client-preparation-20261115.json)绑定提交、官方指纹、命令、JUnit与失败。每轮先提交和Git归档，重算归档官方指纹并核对SQL/补丁等额外文档输入哈希，测试前后源码和输入均不变。候选1670ba9的源码指纹为 `eff5a0e9663fad9a386cd7c81e4ff6c74ef1f2db9e939e21da3e9610ecc7b9a7`。

接下来仍须落实：Owner/机器限权API与原Engine组合、原Runtime接入和服务生命周期，systemd/Compose/TLS与Secret边界，真实独立进程和故障恢复，DB/CAS/密钥的实际加密备份及隔离恢复演练。只有这些正式检查通过才交付SA20本地工程；来源、付费、编码、外发、发布和长期运行各自另验。

当前主库55435仍44份迁移、校验和匹配、无运维表；仓库正式迁移49份、Source产物引用584条匹配。本阶段隔离PG/会话均清理，原H06和其他CLI资源保留；原独立报告、用户未跟踪文件和.env字节不变。没有安装宿主服务、修改实际TLS配置或激活平台/模型/发送/发布。

本轮不需要再次执行旧通过范围。可复现的新阶段入口为 `make verify TASK=SAV1-20-PREPARE` 与 `make verify TASK=SAV1-20-CLIENT`，应显式指向隔离测试环境。正式 `SAV1-20` 整卡入口待真正实施并补齐检查后注册，不能用阶段入口冒充整卡验收。
