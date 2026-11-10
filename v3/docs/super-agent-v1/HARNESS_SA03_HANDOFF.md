# Codex → Trae：SAV1-03 固定交付

source-policy-v1 实施 `9f64dc9000879ae84a71640a6d96bdb4668b332e`，注册验收 304 PASS（SA03 88 项，0 failure/error/skip），lint/typecheck 208 files PASS。源码 SHA `bdd46499b40c3acaa0827c356393583e39ad2f527bb82cf2c0a074d4cce7ca40` 前后一致；结果 `evidence/SAV1-03/20261110T164017Z-e07013dc/result.json`。接口和限制见 SA03_POLICIES.md、config/source-policy-v1.json、accept/SAV1-03/accept.json。

已按实际确认 4ad86c1 §15、f61a233 §16 实施 0032_source_policies.sql、source-policies 管理 API 与受限 source-policy Run/tool/lease/Action fencing。api.py 仅新增 import/register，agents/tools.py 仅新增一条 pre-charge capability guard；Engine 原 reserve_call/settle_call 等财务算法 AST 原样。旧迁移 0001–0031、SA01/02 artifacts、Harness 文件未改，没有导入你的活动工作。

四层策略独立，disabled 接收开关仍独立，shadow 包括显式请求；模式/身份不构成旧 grant/发布/支付授权。策略由当前控制台主人以 hash/UTC/review_ref 签署，版本 CAS、历史不可变、撤权不可复活。预览完全只读。

关联授权冻结 policy/身份 revision；修订/撤权/身份/来源配置变化使原 epoch/lease/工具与待发消息失效。在途 read/effect/unknown 与财务预占仍保留真实回执/结算，不自动重发、不复活任务。通知精确绑定旧 Action/body/目的地/披露及 quiet/rate，仍需旧审批；SA03 不授予 Draft PR 发布。

当前运行桥只给已有 fixture 调查加限制；实际 Source 创建任务与 Coding Worker 留在 SA04/SA13。SA07 未提供共享预算前 model_call 明确拒绝；没有真实模型实验。没有生产 policy、平台监听、模型、Worker、发送或发布，所有 live 门 NOT_RUN。完整进程停止、语义披露与数据生命周期仍须后续验收。

本卡完成后等待用户独立复验，不自动启动 SA04，也不要求 Harness 越过其当前独立验收。0033 尚未预占，下一共享接口/迁移仍需另行实际确认。此文件准备不表示完成消息已送达或 peer 已验收，协作状态另记。
