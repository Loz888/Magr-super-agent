# SA22 模型问答与隔离编码实验接线

2026-11-16：**服务已启用，真实问答及编码产物待新群消息实测；SA22 IN_PROGRESS**。
飞书长连接、回环 API、供应商凭据及模型可用性已核对。当前新增推理0次、费用USD0/2；
不能将入口启用、供应商GET或受控检查记成真实模型问答／编码通过。
SA23未启动；H12真实质量FAIL、MVP未放行保留。

## 授权与依赖

用户明确要求接通模型问答和自动编码，并在收到H12第47节FAIL回执后，以项目负责人
身份明确允许开放实验；见[例外ADR](adr/SAV1-22-owner-authorized-live-experiment.md)。
本轮无需再批准同一范围。限Bot群本人、Loz888/MAGR-v3的固定已提交代码，问答、路由、
Coding和验证共用原SA07累计USD2；该额度不并入H12的旧USD12授权。

已核对Harness确认`efb09f4fedb35c4bc7f579dda0b7e64d2e3111be`第48节。
正式0054 SHA256为`094909bf76fe89917c4733882719f389003208ffdec4baa196461ea609ef6f48`。
只新增精确Owner实验授权事实并前向扩展原绑定守卫；没有Engine/API/Settings、费用算法
或新HTTP接口变更。R05只读锚点仍为08cb1e2／8d5052c，未导入H12/R06活动树。

## 接通范围

问答采用新的原Run图`source-model-conversation-v1`，不伪造Issue。单轮有界仓库上下文
只读取固定Git对象。当前来源、本人角色、canonical正文、显式@、策略和模型／通知
许可均有效才允许调用。原Engine预占／结算、SA07总预算、原Action/Outbox和Dispatcher
保持权威；模型输出不是独立核验结果。答案密文与短期密钥分离，派发前重新鉴权。

明确以“请修改／请修复／请实现……”等开头的编码请求，才可交给原SA13/SA12隔离
Worker。当前仅修改`v3/src`内已有UTF-8代码文件；新建文件、任意Shell、依赖安装、
原工作分支写入、推送、PR、合并和发布关闭。最多一个Worker，每次最多8模型调用、
24工具调用、180秒及USD0.30，仍受共同USD2余额约束。无消息不周期调用模型。

固定H11片段的测试summary只作诊断，独立Verifier结果保持incomplete，业务Task不
因此完成。即使smoke绿色，产物仍须审查；本轮不冒称拥有R05完整独立回归Runner。
完整边界见首次检查前冻结的[实验协议](SA22_MODEL_TRIAL_PROTOCOL.md)。

## 实际部署

专属接入PG由50前向升级到51份正式SQL，全部校验和匹配；SA主库仍44份，未升级或
重启。唯一业务写者仍为同一原Engine。当前Source revision4、飞书config revision2、
Ingress generation4、policy revision4、通知revision3；组织／项目预算各revision1、
lifetime上限各USD2。修订执行配置没有重置账本或预算；旧policy3实验事实保留且失效。

目标上游固定`efb09f4fedb35c4bc7f579dda0b7e64d2e3111be`。独立快照包含全部已提交
`v3/src`代码、元数据及固定smoke/conftest共247文件；不读取Harness未提交正文。
当前manifest SHA256：`e205b9085cd189a61ae6281bd951c966e9742e912779908e62f013dc5b7228e1`。
真实运行镜像：`sha256:587cc5cbaf0a25f09e459a72d3f9b88aed939867cbf72d40b36a88d7999a1208`。
使用Python3.12.16及锁定依赖，原Broker维持无网络、只读根、非特权用户和受限资源。

固定命令为`python3 -m pytest tests/test_harness_v4_h01.py::test_reference_loop_reads_edits_and_runs_real_tests -q`，
工作目录`v3`。它是目标提交已有的受控运行循环smoke，**不是目标需求的独立回归**。
已在原隔离Broker实际运行1PASS、exit0；原始日志包含依赖及只读pytest缓存警告。
该底层receipt的tests_run计数为0，pytest输出为1passed；不据summary提升质量状态。

模型固定`deepseek-flash`、价格版本`deepseek-v4.1-flash-1017-peak-20261116`，输入／
输出每百万Token保守采用USD0.30／1.20；按峰值cache-miss计预占，非独立账单核验。
依据[官方价格](https://api-docs.deepseek.com/quick_start/pricing/)及
[官方接口](https://api-docs.deepseek.com/api/create-chat-completion/)。
本轮真实`GET /models`返回200且包含该模型，未发问题／仓库正文、未调用推理。

监听只处理新策略下的新输入，不追溯执行引导期旧消息。当前正文可读TTL1800秒，
保持原2026-11-17 12:04:22上海时间到期，不延长授权。依赖本机／WSL在线；未启用
自动重连，不属于72小时运行成绩。重启只重建精确已有授权，不自动重放未知调用。

## 检查与版本

产品源码候选`5dc46ae70a9dcc85c9c899d2d32cb73eba92daff`，源指纹
`04e2d2b86fad3c2291df110f1779c321e88f5950f313f3c6e4c1fc0adacc2165`。

| 候选 | 本次范围 | 结果 |
| --- | --- | --- |
| 0528bc6 | 新登记20项首次检查 | 4PASS／16夹具ERROR |
| 2745484 | 仅原16项ERROR | 3PASS／13FAIL |
| acd4d35 | 仅原13项失败 | 11PASS／2FAIL |
| 5dc46ae | 仅原2项失败 | 2PASS／exit0 |

20个独特目标均有分版本PASS，无已通过目标重跑，不称最终同版本20项全量通过。
覆盖原PG账本、撤权／发送边界、unknown不重发、加密期限、通知回执及真实Docker／
Git编辑和大仓库稀疏恢复。lint与typecheck在2745484通过（337文件），之后按用户
要求未重跑已通过静态目标。原回复11项及SA/Harness历史通过项也未重跑。

部署阶段的独立失败另存：仓库大小写、dataclass导出、缺省测试不存在、继承scratch
镜像缺Python loader、就绪探针epoch、通知DTO／profile及保留Scheduler revision的
初始化冲突。修正后只检查对应失败启动／运行目标；没有为这些修正重复20个已通过
目标。所有初始记录、旧manifest、旧实验事实和镜像标识保留。

证据根目录：`evidence/super-agent-v1/SAV1-22/model-coding-20261116T053304Z-082b98a4/`。
`versioned-checks.json`列出逐例／版本；`initial/repair1/repair2/repair3`保存源码归档、
命令、JUnit和失败。`deployment/`保存启动、实际Broker、迁移、供应商只读探针及部署
helper归档，归档只含代码，不含私有配置或密钥。本卡测试PG已清理，真实接入PG与
服务作为有意保留的试用资源继续运行；用户文件、`.env`和其他CLI资源保留。

## 使用与尚待实测

在Bot群`@MAGR助手`提问；仓库问题尽量给具体路径。编码使用例如“请修改
v3/src/mgar_v3/__init__.py，仅在隔离副本末尾增加一行注释 # SA22 coding probe”。
这是最小接线探针，不作为真实修复质量任务。产物位于私有控制目录
`.local/feishu-bootstrap/coding-control-v2/artifacts/<run_id>/patch.diff`及`result.json`。

已发出新群消息核验请求，目前尚无本阶段真实模型问答／编码输入及反馈证据；两项
均NOT_RUN。通知已有前阶段真实固定回复证据，本阶段动态答案投递尚待核对。
完整SA22六门、10正向任务、独立验收、72h／72h／5工作日与发布均未放行。
机器摘要见[model-coding-20261116.json](accept/SAV1-22/model-coding-20261116.json)。
