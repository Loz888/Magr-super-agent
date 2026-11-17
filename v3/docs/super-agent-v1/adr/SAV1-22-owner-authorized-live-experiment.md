# ADR SAV1-22：负责人明确授权的受限真实模型与编码实验

状态：ACCEPTED FOR LIMITED EXPERIMENT IMPLEMENTATION，2026-11-16。

用户要求接通模型问答与自动编码，并在收到 Harness 第47节回执后明确表示：
“作为项目负责人，我允许开放后进行试验”。本决定记录这次对既有质量前置的特例，
不要求用户再次批准相同实验，也不把 H12 真实质量 FAIL 或 MVP 未放行改为 PASS。

范围沿用 Bot 群、本人、Loz888/MAGR-v3（本机 magr-harness 的固定已提交代码），
问题正文及授权仓库代码可入模型，问答、路由、Worker与验证共用新增累计 USD2 上限。
使用原 Engine/PG、原 usage_calls/SA07、原 Assignment 与隔离 Broker/Verifier。
编码只生成独立工作区产物；推送、PR、合并、部署仍关闭。

问答走独立版本的原 Run 和通知因果链，不伪造 GitHub Issue，不把模型文本当核验事实。
数据、密钥、结果和待发内容服从当前身份／策略与期限；未知付费／平台结果不重发。
实验只处理激活后新输入，不追溯执行引导阶段的历史消息。

真实编码需保存精确 Owner 实验授权及当前 Source/策略、profile、仓库基线、R05证据、
manifest 和期限；SQL默认拒绝无该授权且质量门未通过的 live Coding。
现有 fixture profile 不升级为 live，旧迁移不修改。0054 提案仅增加授权事实并扩展
0046绑定守卫中的质量分支，正式SQL须先完成共享编号／所有权协调。

固定依赖为 R05 源码08cb1e28fac1c5e2e27117f7de62fbd3c20d0ac2、交付8d5052c，
工程／恢复证据可审查；没有通用真实质量或任意仓库环境通过结论。
SA侧当前固定H11切片与R05完整Harness存在差异，实验支持范围以本次实际接线与检查为准。
不导入未提交R06工作，不代替H12 R08新持出验收，不将本次费用转入其旧USD12授权。

SA22完整六门、独立验收和长期试用继续分别报告；实验启用不表示整卡完成或产品发布。


2026-11-17 本人更新：累计上限改为USD10（含历史消费），0055已按Harness第49节确认
并仅安装专属Bot PG。本人进一步明确要求启用思考并设置max；新增独立模式manifest，
不覆写原trial。按原数据／预算／期限接入问答及Coding的thinking enabled与max参数，
思考工具历史由原加密响应记录同attempt恢复；不改变H12 FAIL、独立Verifier或发布门。
旧unknown及全部失败保留。新局部preflight可证未发送时由原Engine结算已知零费用，
不回溯修改既有unknown。见 SA22_THINKING_MAX_20261117.md 和分版本原始证据。
