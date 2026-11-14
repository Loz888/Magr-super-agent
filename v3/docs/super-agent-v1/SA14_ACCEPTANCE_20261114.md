当前更新：独立验收FAIL（SA14-I01）。私有修复e88ff0c已仅原两失败2PASS，等待独立复验，未放行SA14。
详见[修复报告](SA14_I01_REPAIR_20261114.md)。下面为原4c2d624／62ac8ad的历史交付记录，原字节已保存。

# SA14 本地受控工程交付

状态：**READY_FOR_INDEPENDENT_REVIEW**。本地工程检查闭环；独立验收 NOT_RUN，SA14 保持 IN_PROGRESS，SA15 未启动。
受检功能候选 `be489f7a12f1ffe0e263cfac07a5b1c2b83abb91`，指纹
`9953c61b234686dfa282297881cb118e44cee7aebce55bb31987838662b74881`，运行前后一致。
最终交付候选与指纹见 [accept.json](accept/SAV1-14/accept.json)；后续无生产源码／SA14测试／迁移／固定Harness变更，
只补当前元数据、八份契约schema和九份旧测试的当前迁移目录43→44断言，原字节均归档。

## 实施与范围

第39节固定确认 `c711d54504c4945625505f47e9df56e22bc1bdac` 已核对。0047、六张附属事实表、
12个metadata/no-store接口及 Engine／API／Ingress 三文件最小接线按批准原字节落地。
SQL SHA256 `9ce372e6a82cbf747c51624486ef7548faf056cc60912e0287279be8b569f041`；
补丁 SHA256 `c43e05c0d8aabe1bccf3484cbb7922c5f6f1897f6d2cd5ac0840b4454ff85b5e`。
旧43份SQL与SA13逐字节一致。只读核对对方已提交交接，没有读取或导入Harness活动树、H12补丁或0045。

原 Engine 同一持锁 PG 连接与 mutex 继续管理业务写；输入、checkpoint、operation_view、Job和事件都是
同一原 Thread／Ticket／Run／Mandate／Assignment／Session／Workspace／epoch 的附属事实，没有新Task、Run或钱包。
原SA13/H03/SA12 bound H06/固定H11受控纯函数fixture范围保留，不提供实际SQL执行、真实模型外发或发布。
默认 SourceAsync.runtime=None、profiles为空；构造和恢复都不启动工作线程、平台监听或周期模型调用。

输入按revision持久化为received／applied／blocked。Source只存canonical引用、版本与权限hash；
精确回复、原生话题或已验证SA11投递关系唯一命中原目标才归属，不把整群变成一个Task。
Source控制仅限当前可信人类、context_provider、task_controller与精确Ticket membership，且不能context_only。
独立完整文本“不要改数据库”只映射服务端冻结的明确deny集合；CI、引用、代码、链接标签与日志不授控制权。
Owner当前会话通过有类型的deny请求收紧，不接受正文、argv、provider、token、paid或成功结论。

安全点重查完整原权威与Source依赖，原子形成checkpoint、消费游标和disposition。实际模型派发／
工具／文件编辑／命令启动前仍检查最新input revision、scope、树和许可。整个旧模型回复被丢弃后可重规划；
仅内核内部快照允许观察当前树以完成丢弃流程，模型选出的读取、修改与命令仍受旧回复版本限制。

受控模型采用prepare／短dispatch／poll显式端口；最终guard直接包住实际派发，等待响应期间释放
Engine mutex。命令Job一对一引用原Coding operation和原SA12资源operation，经原bound Broker启动、查询与停止。
cancel先记录stop_requested，只有实际owned停止回执才known；unknown保留、不重发、不释放未知资源槽位。
实际费用仍走原usage_calls／SA07项目与组织预占，撤权后的已发响应照实结算，不退未知费用。

Source撤权、编辑、删除和角色变化在原事务sticky fence并擦除受影响原Coding密文；
operation_view记录所有已应用Source依赖，密文TTL取最短期限，解密前再验。恢复只保留原事实、
推进旧epoch并对账原资源，不自动恢复旧执行权或重发模型／命令。SQL事务内没有文件、网络或Docker IO。

## 分版本检查

| 范围 | 不可变候选 | 结果 |
| --- | --- | --- |
| 正式SAV1-14首次 | d635470 | 61项：26 PASS／35 FAIL |
| 单个原运行失败诊断 | 5758b31 | 1 FAIL，超时日志保留 |
| 同一个失败用例性能剖析 | 5758b31 | 1 FAIL，约5.9万SQL及profile保留 |
| 仅原元数据／API／恢复29失败 | 5758b31 | 29 PASS，含三处真实SIGKILL |
| 仅原6运行失败 | f8b864d | 1 PASS／5 FAIL |
| 仅剩余5运行失败 | be489f7 | 5 PASS |
| 新原子编辑边界首次 | be489f7 | 1 PASS；与上述5项组合为6 PASS |

62个正式独特目标均有对应版本的PASS；原26项和所有后续已通过项没有重跑。
原私有65项与SQL草案29项仍是历史准备证据，不替代正式集成，也未重复。
不声称最终候选单轮62PASS或单轮156PASS。每轮执行前均有不可变提交、归档及与工作树一致的源码指纹；
每个目标与完整命令见[逐例账本](accept/SAV1-14/case-ledger.json)及[机器摘要](accept/SAV1-14/accept.json)。

首轮失败包含fixture重复创建唯一管理员、Bob缺少requester而被原Ingress正确判为context_only、
TTL探针自身耗尽保留时间及恢复查询使用不存在的budgets.run_id。修正探针／fixture，未放宽生产控制权限。
运行诊断定位多层guard重复全量SQL校验：改为同一Engine mutex内复用当前操作的完整原权威，
事务提交后才执行IO，仍保留原工作区post-IO复查及实际派发guard，不跨操作缓存授权。
启动／收尾探针改为45／60秒以容纳原120秒Run预算内真实PG校验；控制提交的3秒响应断言未改。

新增边界在第一份真实文件atomic replace后提交限制：原两文件批次完整收尾，原operation成为known，
实际postimage／tree／费用保留；下一项不兼容写入被拒绝。三处真实SIGKILL分别发生在输入提交后、
受控模型实际派发后及owned Docker Job运行中；恢复不丢输入、不重复命令、不退unknown预占。

lint／typecheck的首轮失败均保留；仅失败目标在5758b31通过（mypy278文件）。随后未重跑这些通过目标，
新原子边界文件的首次lint在be489f7通过。后续运行修复与最终元数据未声称再次通过全库静态检查。
首轮外层wrapper因Make错误尾文解析JSON失败，真实注册result/JUnit已完整写出，原日志、人工摘要和cleanup保留。
一次原崩溃探针失败后留下的精确本卡容器已按marker/ownership清理，未清理其他用户或CLI资源。

## 安装、完整性与交接

本副本主库只增加已确认0047；44份迁移checksum一致，旧43不变，六表全0行。
默认runtime／cipher／profile／driver均未注入；无新增线程，隔离PG／会话、本卡探针容器和crash actor无残留。
受保护用户文件及.env保持原指纹。原环境allow_paid／allow_external_write为true，未修改；这些开关不构成授权，
本卡测试子进程禁用付费与外发，没有真实平台、模型网络调用、通知或发布。

[迁移安装](../../evidence/super-agent-v1/SAV1-14/integration-20261114T081658Z-10a31715/main0047-install.json) ·
[默认状态与清理](../../evidence/super-agent-v1/SAV1-14/integration-20261114T081658Z-10a31715/main-inert-and-cleanup.json) ·
[最终字节与产物审计](../../evidence/super-agent-v1/SAV1-14/integration-20261114T081658Z-10a31715/final-integrity.json) ·
[独立核验请求](SA14_INDEPENDENT_REQUEST.txt)。

本轮仅本地受控engineering通过。完整系统安全、真实来源、真实Coding、付费、外发、发布及长期门仍NOT_RUN；
本副本固定H12真实Agent／MVP的历史FAIL保留，H12验收由另一CLI继续。SA14待独立验收，不启动SA15。
