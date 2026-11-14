# SA13：Coding Worker 与 Mandate 接线

状态：DONE（本地受控工程及用户接受的独立闭环）。用户已接受 SA12 独立闭环并授权本卡；当前完成固定依赖审计、
私有契约／校验器和具体共享提案。Harness固定617e14a第38节已确认，原批准0046、四文件接线及固定最小内核已精确应用；本地受控工程已完成分版本检查，等待独立验收；SA13仍IN_PROGRESS，不启动SA14。

H11 固定 d3f3457 的工程／恢复范围有独立证据。H12 固定观察 88c75b8 的真实 Agent／
MVP 门仍 FAIL，新的 Source Coding 门保持关闭；模型连通或环境恢复不替代该门。
旧调查只读 grant、旧公开 API、原财务函数及 SA09-I01 发送前校验保持。

本片复用原 Engine、Task/Run/Mandate、SA12 Assignment／资源意图和 SA07 共同预算。
不直接套用会创建新任务或额度的 Harness Store／Ledger。新增 Mandate 使用明确的
source-coding-mandate-v1，旧 Task 不升级。Worker 不获得主人 cookie 或安装令牌；
命令经 SA12 bound Broker，测试产物需真实代码树、当前 epoch 和可信 journal 绑定。

固定 H11 adapter 已放入新的 source_coding_adapters 模块，保留旧 reference adapter。
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
SA12，固定确认617e14a第38节现已完成。0045仍归Harness，0046归SA；0046已安装，主库43份checksum一致，六新表0行；运行检查使用独立PG。固定提案文件保留原始草案状态，不以其文本状态替代实际交付记录。

真实来源、付费、外发、真实编码、发布、语义和长期门均 NOT_RUN；SA14 不启动。

当前正式实现把模型端口限定为显式 ControlledCodingModel，普通网络模型适配器不能直接注入
fixture profile；没有默认 Worker 或 Provider。受控首片仅开放现有 `.py` 文件的纯函数
算术子集，禁止导入、属性访问、装饰器、打印、主动退出及替换测试运行器；固定原测试
目录只读。服务端固定命令与工作目录、真实验收要求一起进入 acceptance SHA，不允许
调用者只提供一个无实际内容绑定的 opaque hash。此限制用于本地工程证明，不是通用
Python 沙箱或真实修复门已通过的声明。

恢复区分控制器与局部 Worker 丢失：完整 Engine 恢复沿原流程暂停 Run、推进 epoch、
排空 SA12 租约，并停止旧 Coding attempt、保留 unknown 财务与操作水位、擦除载荷。
不会在旧 epoch 下自动续写。原控制器及授权仍有效时，局部写入恢复端口只接受精确
pre/postimage 混合，遇到外来内容拒绝覆盖。逐文件 atomic replace+fsync 防止截断；
整个多文件操作仍由持久意图和恢复事实描述，不假称文件系统多文件原子事务。

正式185项及27新增边界均有分版本PASS证据，原失败完整保留；详见[工程交付](SA13_ACCEPTANCE_20261113.md)。准备阶段主库42的记录是历史快照，当前为43。显式maintain端口在拒读事务之外提交当前权限失效／TTL清理，保留最小水位且不可恢复密文。

2026-11-14：用户已接受固定f53f423／c49cf1f的独立32PASS，见[关闭记录](accept/SAV1-13/independent-closure-20261114.json)。上文“等待独立／不启动SA14”为历史工程交付时点；现已明确授权启动SA14，证据限制与真实门不变。
