# SA13 0046 六表草案

状态：DRAFT_NOT_APPLIED_NOT_TESTED。0046仅为候选编号，须与Harness确认；0045仍归Harness。
本文件和SQL均未进入正式迁移目录，未连接数据库、执行SQL、运行检查或启用Worker。

PG、原Engine同一advisory lock／mutex／connection仍为唯一业务写者。SQL只增加六类
伴随事实；原Mandate、Thread、Ticket、Run、SA12 Assignment及原usage_calls继续各自权威。
本稿不修改原42份迁移，也不导入Harness0037／0040／0045。

| 新表 | 存储及边界 |
| --- | --- |
| sa_coding_bindings | 原五类ID、Source canonical／身份／principal／策略版本及review／profile／context／Mandate哈希。只准全新source-coding-mandate-v1原Mandate与source-coding-v1原Run；一Run一绑定。只允许admitted→revoked并推进CAS，无第二Task或grant内容。 |
| sa_coding_attempts | 精确原SA12 Assignment、Session、epoch、workspace identity／实际初始树／环境及profile；ready→running→stopped或result_recorded。状态仅描述本次Coding运行事实，不能直接完成Task、释放slot或发送消息。 |
| sa_coding_operations | 有界稳定step／request哈希意图及queued／running／unknown／known事实。模型必须引用同原Run的usage_calls预占，并已有SA07 worker归属；无新金额或Token列。执行命令须引用原SA12 start／job intent。 |
| sa_coding_payloads | 可选model_response／checkpoint的有限ciphertext及哈希／长度／到期／删除水位。1MiB明文上界，密文1.6MB上界，最多1小时且不得越绑定Source期限。只允许一次不可逆清空ciphertext，不能延长TTL、更换内容或恢复。 |
| sa_coding_verifications | 可信journal operation、Mandate、精确实际树、环境、profile、acceptance及provenance哈希；只在原当前epoch／有效Assignment和running attempt下新增。passed另须关联同范围成功execute操作及原SA12真实成功终态；无日志／正文，也不自行提升原Task成功。 |
| sa_coding_events | 固定事件枚举、原绑定/attempt/op引用与有类型的allowlist元数据。state只能固定枚举、counter只能有界整数、hash只能64位小写SHA。事件不可变。 |

原Mandate.scope完整保存私有`CodingMandate`模型JSON：schema_version和scope_kind均为
`source-coding-mandate-v1`，原target五类ID、origin及其canonical引用、repository、grant、
budget、harness、context_refs及固定哈希、签署人和时限均由受信Engine构造。
原Mandate.policy_version同为`source-coding-mandate-v1`；旧调查scope和旧bind_run不改。
草案guard对完整模型字段与展开ID/哈希逐项核对，不依赖额外JSON包装或新增read_only字段。
实现须明确把新版本原scope判作独立Coding授权，不能通过缺省read_only=False升级旧scope。

`mandate_revision`为冻结原CodingMandate版本；`revision`为绑定撤销CAS版本，两者不可混用。
新绑定只接受queued、epoch0、未开始的原Run；原factory、绑定、关联去重及SA12 enqueue必须
在原同一外层事务原子提交，不能让旧API supervisor先捞到尚未managed的新graph。
已有旧调查Task/Run不能通过本表换Mandate或转图；GitHub精确新资源可复用既有winner factory，
无native proof的飞书不能伪造Issue ID或绕过原关联创建规则。新无资源协议需另行明确。

每个可更新表有独立trigger函数，没有用跨表CASE访问不同record字段。公共
`sa_coding_current`只查询共享的当前关系／版本／时间栅栏；它不是签发权，也不代替完整
Source接收、角色、路径交集、snapshot、上下文外发许可或预算校验。Engine必须先用实际
当前PG事实和私有strict模型验证所有嵌套字段与哈希；尤其不能接受HTTP／模型自报的harness
PASS、trusted provenance、原Mandate内容、Owner身份或付款许可。

所有模式要求H11 engineering／recovery和H12 engineering分别PASS；live模式另要求
H12 live_agent／mvp均PASS，不要求H11并不存在的live_agent／MVP门。
固定H12当前真实Agent／MVP为FAIL，因此本提案不能开启真实Coding。Gate字段只冻结被
复核事实，受信服务仍须将其与固定已接受清单和当前注册profile精确比对；SQL常量本身
无法证明外部证据可信。模型连通、环境恢复或受控fixture成绩不替代真实Coding门。

新IO前检查当前Source canonical head／无歧义／未删除／正文TTL、身份及策略版本，并要求
原Run epoch、活动Assignment lease、当前Session和签署容量配置。实际派发仍须走SA12
持久容器身份与最后dispatch_guard；`LocalExecutionBroker`和原随机名execute路径不得选用。
未知写/执行结果只能对账，不能unknown→running。迟到known receipt允许保留资源和原费用
真值，但不能新建验证事实或重启旧attempt；原model settlement必须先真实完成。
原模型预占与SA07归属、Coding model intent须在同一事务提交；不存在先建新钱包再对账。

execute Coding operation与对应SA12 resource operation规范化同一实际请求，request_sha256
必须相同。verification operation自身不挂resource_operation_id；其独立request hash描述
验证请求。验证事实另用execution_operation_id指向同attempt／coding／project的真实命令，
passed要求该op为known／ok的tool execute，并关联known_terminal／succeeded的原SA12资源
回执；实际树和环境必须与当前验证相同，原执行观察时间不得晚于验证。failed／incomplete
可不指定execution引用；孤立verification op或provenance hash本身不能得到passed。

SQL对ciphertext只约束形式、身份、寿命和不可逆擦除，无法判断是否确实加密，也不能阻止
持有数据库最高权限者读取字节。实现必须使用服务端密钥做认证加密，密钥/凭据不入表；
每次解密先重查Source/身份/策略/原目标/Assignment，并绑定aad至coding/attempt/op/context。
Owner撤权、Source编辑/删除、到期及恢复旧备份时应先失效读取并执行同Engine擦除，不把
密文、checkpoint、工具输出或Source正文放入普通事件、run_inputs.question或永久CAS。
此服务接线尚未实现；本稿不声称现有Source清理器已管理这些新载荷。

六表仍需要实际同conn实现和首次独立PG安装/约束/并发/回滚、权限/擦除恢复测试，特别是：
旧scope拒收、跨ID/project/session/epoch混绑、当前Source变更、unknown拒重发、旧验证拒用、
原usage/SA07原子性、载荷延寿/复活拒绝、typed事件正文注入，以及原真实Broker释放资源门。
草案阅读和DDL文本都不能记为工程PASS。
