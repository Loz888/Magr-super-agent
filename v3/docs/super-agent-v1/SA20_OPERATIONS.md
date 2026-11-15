# SA20 常驻部署与运维

SA20保持IN_PROGRESS，等待独立验收。Harness第46节确认`1c4f494442d2d5fc8d4c1d35856cc91c922cc70e`已核对，正式0053四表、原Engine/API/Settings接线及11接口已落实。[工程交付](SA20_ACCEPTANCE_20261115.md)和[分版本摘要](accept/SAV1-20/accept.json)记录最终候选与证据；[部署、故障与备份手册](../../deploy/super-agent-v1/README.md)给出具体配置与恢复步骤。

本地受控实现包括独立Source relay、Scheduler/Worker/Dispatcher/Watchdog控制进程、HTTPS网关、原Engine唯一持锁连接、原SA12/SA14/SA17端口、组件scope/到期/boot/generation/sequence、持久tick与unknown、元数据指标及Owner wait/escalate决定。项目命令继续由既有H03/H06隔离Provider运行；受信业务编排仍由Controller承载。默认Worker/平台Runtime未装配，运维开关不创造业务/付款/通知/发布许可。

来源在线、权限不足、gap、积压、原语义进展、Job、Assignment、Redis与unknown分开投影；心跳不会生成Run进展。快照最多16当前组件、32来源和32 Run，超限显示partial。保留的旧组件不会挤掉当前配置。Watchdog提交要有当前boot/generation，Engine重新核对scope与事实cursor。告警只保留无正文依据，Owner的wait/escalate不会控制任务、退款或重发。

真实环回HTTP/TLS、独立进程及两个SIGKILL边界已有工程证据；原SA14工作在HTTP客户端退出后完成实际Git工作区修改与Docker测试，原SA17通知丢回执保留unknown，签名来源通过独立relay提交后ACK，实际Redis实例停止后PG队列仍在。平台HTTP与模型内容使用受控fixture；这些成绩不等同真实飞书长连接、真实模型编码或在线服务器整机验收。

加密备份调用实际PG16 pg_dump/restore，绑定DB、CAS、Source正文/SourceConsole/旧Web三种密钥、项目及全部迁移SHA。只支持显式静默/离线安装，拒绝活跃Engine、无效密钥、链接、非规范CAS、完整性故障与已存在目标。恢复进入新空库/目录，原消费者密钥路径可用，unknown账本保留，默认入口在打开Engine前拒绝`restore-hold.json`。恢复后的撤权/删除、旧远端写和费用仍需操作者核对，工具不会自动重新激活。

正式55个独特用例及此前70契约/草案、18客户端检查均有分版本PASS，修复只复验失败项/目标。正式入口为`make verify TASK=SAV1-20`，本轮没有在最后候选重复运行全部55/143项。阶段准备overlay应在其原源码归档复现，正式入口不重复安装草案。静态成绩同样按版本/作用域报告，最后的盘点不替代功能测试。

共享接线先按确认补丁完整应用；随后仅把新增API import排回正确排序，修复首次接线lint的I001。[精确样式增量](sa20-proposal/shared-wiring-style-delta.json)与原补丁均保存，SQL草案/正式副本哈希仍一致。

部署材料：[独立Compose与systemd](../../deploy/super-agent-v1/)、[角色样例](../../deploy/super-agent-v1/roles.example.json)、[TLS样例](../../deploy/super-agent-v1/https.example.json)、[备份](../../deploy/super-agent-v1/backup.example.json)、[恢复](../../deploy/super-agent-v1/restore.example.json)。私有配置文件由对应进程账户独占0600，平台/业务秘密不交给角色控制进程。

主库55435维持44份迁移；正式仓库50份。没有安装当前宿主服务、导入H12或开启真实来源/模型/外发/发布。固定H12真实Agent/MVP FAIL保留，全部真实能力、全系统安全及长期运行门NOT_RUN，SA21未启动。
