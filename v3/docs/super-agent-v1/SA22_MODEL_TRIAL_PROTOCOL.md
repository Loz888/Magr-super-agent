# SA22 模型问答与负责人授权的编码实验

本协议实施用户2026-11-16的真实模型问答／自动编码请求及其明确的质量门例外。
范围为Bot群本人、Loz888/MAGR-v3的固定已提交代码，问答与Coding共用原SA07累计USD2。
模型供应商为现有DeepSeek配置；模型与价格固定到激活manifest，按官方峰值、cache-miss
价格保守预占。没有消息时不调用模型；unknown保留预占且同一步不重发。
本文在首次检查前冻结，实际成绩／启用情况另记，不由协议或目录状态推断通过。

问答使用新的原Run图`source-model-conversation-v1`，不伪造GitHub Issue。
当前Owner、来源、当前canonical正文／身份角色、显式@、model_input、模型能力和
仓库／基线／路径、通知授权均需有效。使用原Engine.reserve_call/settle_call与
原usage_calls/authorization_budgets/budgets/SA07；没有第二财务账本。
预占提交后，在物理发送前按同一Engine互斥锁重查最新状态；HTTP在SQL事务外。
真实用量先结算，再解释输出或接受结果。异常／丢回执不退款、不盲重发。

仓库问答只读取固定Git对象，不读取Harness未提交文件；当前为单轮、有界上下文。
问题、代码和历史资料不是授权；模型没有Shell、发布或直接发群工具。
模型回答不是已核验修复。输出经严格JSON／类型／字节检查，去重键绑定原输入。
原Action中保存含call_id与正文hash的不透明引用，结果密文在原已知用量行中；
每条输出使用独立短期私有密钥。发送前重查当前来源／策略／ACL／配置和正文hash，
在原消息下解密回复。密钥按TTL删除；过期／撤权不可新读取或派发。
必要故障／预算说明为确定性状态模板，不把unknown调用伪记为成功答案。

0054依据Harness efb09f4第48节，精确SQL SHA256：
`094909bf76fe89917c4733882719f389003208ffdec4baa196461ea609ef6f48`。
新增表只记录Owner对特定Source／策略／profile／仓库基线／R05证据／manifest／期限
及USD2上限的实验授权。0046历史不改，绑定守卫保留其余检查；无授权的live仍拒绝。
H12真实质量FAIL／MVP未放行不改为PASS，fixture profile不升级为live。
没有Engine/API/Settings／费用算法改动或新HTTP路由；SA私有Scheduler仅增加经当前
Coding实验授权校验的live来源通路，普通legacy live Run仍不能进入该通路。

编码使用既有SA固定H11切片、原Mandate/Run/Assignment/lease、H03隔离工作区、
真实隔离Broker、持久意图及Verifier。R05只作为具名工程依赖证据，没有导入R06活动树，
也不冒称本片拥有R05完整独立回归Runner。
首版实验仅修改授权范围内已有UTF-8代码文件，固定测试及元数据保持保护，项目进程
无网络或宿主密钥；最多1个Worker。普通问答不运行Worker，明确“请修改／请修复”等
编码请求才可交给实验工厂。当前代码片采用非thinking工具协议，未接入R05 reasoning状态。

大仓库写入意图保存触及文件的完整pre/postimage和其余文件hash，所有文件和全树
仍须匹配；不把整仓正文塞入1MiB加密记录。恢复只接受精确pre/postimage混合，
外来变化拒绝覆盖。Continuation从H03受保护基线按hash重建未变文件。
原Scope／ACL／epoch／lease／预算与每次读写、模型和命令重新核对。

实验可以生成实际代码差异与运行固定smoke检查。固定H11切片的summary计数只作
诊断，即使返回绿色也不产生独立验证PASS；产物停在待人工审查，不完成业务Task。
原分支、PR、推送、合并和部署关闭。无法准备依赖、范围外路径、新建文件、任意
命令或未通过检查须明确报告未完成，不虚构“已修复”。

新登记`SAV1-22-MODEL`只运行本阶段新问答／原PG／受控HTTP／实际Docker实验用例。
原回复11项、SA00–SA21与Harness历史通过项不重跑；修复只复验实际失败节点／目标。
真实平台、真实模型、实际代码差异、独立质量、发布与长期运行结果分别记录。
本协议不宣称SA22完整六门或H12质量已通过。
