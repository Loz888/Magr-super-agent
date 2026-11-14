# SA15 可鉴权 Chain of Recall 首片

状态 IN_PROGRESS。SA14 598f760／cfc217e已获用户接受独立本地受控工程通过，I01关闭；不重跑其通过项。
范围是原Engine/Context/Memory/CAS上的只读检索，不建立第二Graph/Task/Run/授权库或缓存正文。

原Source/input、Thread、Ticket、Run、SA14 Job、Evidence、Artifact、Memory、Review/Action和Delivery/Outbox
通过既有PG键与已验证关系构成虚拟有向索引。关系不是授权；每跳重新验证目标项目/仓库、当前原Source
安装/身份/角色/Receive/正文保留期/canonical head或原legacy actor，非授权节点/边不返回ID或正文。

Owner沿原当前cookie/Origin-CSRF；Worker沿原当前Run/Assignment/Session/epoch与Mandate。首片Worker仅召回
同一已授权安装/会话内其他任务及原目标引用；跨私密群/其他安装/仓库默认拒绝。Owner查询依原Source管理
读取边界，不把Owner许可传给Worker。只贡献日志的人不能借检索更改授权或任务状态。

resolve/walk/search默认只返回有界元数据，页读取必须有从原Source/Task/Evidence起的真实provenance路径。
不开放任意CAS hash、宿主路径、URL下载或服务端自报身份。原文读取发生在原短PG检查后、CAS IO在事务外；
IO后和释放内容前再次检查所有当前来源/权限/version，任何变化拒绝该旧结果。
来源过期/编辑/删除有最小unavailable/historical事实，Source原文不另存Memory/CAS；无原证据的摘要只作数据。
派生Artifact/Memory内容需要其全部Source依赖仍可读，不由可见的公共父节点绕过私密引用。

遍历最多4跳/32节点/128边/16KiB，原文页128–4096字节，UTF8边界不拆；每轮候选扫描、PG语句及整体操作有界。
分页opaque加密游标绑定当前caller、query/path、对象version/hash、offset与短TTL；被跳过的私密候选ID不明文出现在游标。
每页都重验，旧epoch、Owner登出、权限撤销、跨对象、跨来源、篡改或过期拒绝；不靠签名游标恢复旧读取权。

“昨天的登录问题怎样了”按显式时段、当前可见Source/任务引用做有界确定性检索；多匹配返回候选与澄清，
没有匹配不猜任务。首轮冻结合成检索集报告逐例/分母，不把规则或fixture检索质量称为真实模型语义PASS。
Memory/Context摘要不授执行/付款/发布权，不通过检索创建新Worker、调用付费模型、通知或写外部平台。

H10固定c140dad的F01/F02独立范围PASS已审计，long_context与本副本既有固定H11字节相同；其他OPEN发现保留。
不导入H10文件registry/上下文正文存储，不改固定Harness Broker/kernel/contracts，不参与H12验收。

拟共享范围只有Engine组合SourceRecall和API注册9条路径；不改财务、恢复、旧授权、Source清理或既有API。
本片复用现有44迁移及其关系索引，不新增/预占0048。若后续发现确需共享schema或额外文件，先补具体提案确认。
SA15正式接线/注册/PG与API整卡检查/独立验收未完成前保持IN_PROGRESS，SA16不启动；真实门均NOT_RUN。
