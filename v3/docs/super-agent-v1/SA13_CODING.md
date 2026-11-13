# SA13：Coding Worker 与 Mandate 接线

状态：IN_PROGRESS。用户已接受 SA12 独立闭环并授权本卡；当前完成固定依赖审计、
私有契约／校验器和具体共享提案。正式 Worker、Engine/API 接线、0046 与内核导入尚未应用。

H11 固定 d3f3457 的工程／恢复范围有独立证据。H12 固定观察 88c75b8 的真实 Agent／
MVP 门仍 FAIL，新的 Source Coding 门保持关闭；模型连通或环境恢复不替代该门。
旧调查只读 grant、旧公开 API、原财务函数及 SA09-I01 发送前校验保持。

本片将复用原 Engine、Task/Run/Mandate、SA12 Assignment／资源意图和 SA07 共同预算。
不直接套用会创建新任务或额度的 Harness Store／Ledger。新增 Mandate 使用明确的
source-coding-mandate-v1，旧 Task 不升级。Worker 不获得主人 cookie 或安装令牌；
命令经 SA12 bound Broker，测试产物需真实代码树、当前 epoch 和可信 journal 绑定。

固定 H11 adapter 计划放入新的 source_coding_adapters 模块，保留旧 reference adapter。
H03 的 resolve、diff、registration ID 与多文件恢复语义需私有 façade 适配；初始范围
仅现有普通文件、受控 fixture bugfix，不开放新建文件、增量 replacement、任意 feature
或宿主执行。没有默认 Worker、模型、线程、付款、监听或发布。

已执行的准备检查：139 个纯契约用例、lint 和 typecheck（262 文件）首次通过；
临时副本固定内核导入与 wire 拒绝预览通过；隔离 PG 中原42迁移加0046草案43份、
六表安装通过，主库原42未变，临时库／会话无残留。以上不构成 SA13 整卡工程、
PG运行、Broker、恢复、独立或真实编码验收通过。Workspace façade 首次13项本地Git/H03检查及新增文件lint/typecheck通过；它只开放受guard保护的读取／快照／差异，缺原持久journal时明确拒写，未冒充真实PG授权或写恢复证明。

具体协调材料已提交 b6079db，见[转发请求](sa13-proposal/ownership-request.txt)、
[范围及六接口](sa13-proposal/SA13_SCOPE.md)、[SQL及补丁哈希](sa13-proposal/proposal-manifest.json)。
AGENTS 要求共享 Engine/API/schema/budget 变更前对齐所有权、接口与编号；第37节只覆盖
SA12，正在等待新的 Harness 固定确认。0045 仍归 Harness，0046 仅拟编号；确认前不改
正式迁移或接线，不导入对方活动文件。

真实来源、付费、外发、真实编码、发布、语义和长期门均 NOT_RUN；SA14 不启动。
