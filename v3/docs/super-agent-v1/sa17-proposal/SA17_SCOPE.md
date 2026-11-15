# SA17 多渠道通知精确范围提案

状态：准备中。SA16 6a8b467 独立原失败1PASS已由用户接受；SA17正式接线、整卡工程及独立验收尚未执行。SA18不启动。

原Engine持锁连接及短事务是唯一业务写者。新增SourceNotifications只组合策略、来源和原Action/Approval/Outbox；不复制任务、消息发送状态机或费用账本。所有Channel IO在事务外。默认无Channel注册、不启动Dispatcher或后台监听，只接受服务端显式注册的fixture profile，真实平台、付费、外发、发布和长期运行NOT_RUN，H12真实Agent/MVP保留本副本原FAIL，验收由另一CLI负责。

0049草案五表仅存Owner签署的配置版本、原Action因果/实际hash及Delivery观测和无正文事件。精确SQL、三个共享文件before/after及补丁、11个Owner路由见proposal-manifest.json。正式文件及接线须新的Harness确认；第41节只覆盖SA16。

四个固定模板对应verified_result/necessary_question/review_available/critical_fault，程序从原Run/verdict/可信CodingVerification构造事实，不接受任意通知正文、日志、URL、模型成功声明或自由接收者。模板仅提示真实状态并指向已认证控制台，不复制原文、日志、代码、问题内容或可复用链接。public_summary仍须显式NotificationGrant，当前首片只向相同Source安装和原话题发，跨来源披露不开放。

配置须当前Owner签署，与确切policy版本/hash、注册profile、目的地/内容类/时限绑定。计划、领取及最后发送前均检查当前Source配置、canonical input/版本/有效期、身份/上下文角色、原任务关联、policy/Receive/NotificationGrant和实际payload hash。ExecutionGrant不产生通知权限。Notification批准仅授予新action_type的具体模板hash，不放宽旧只读/评论/Draft PR语义。

发送仍走原Dispatcher和Engine.claim_effect/effect_result/reconcile_effect。最小Engine hook将通知Delivery记账与原业务Task完成分开；原评论/PR保持既有处理。quiet/频率/冷却在领取前延期；聚合有界，持久幂等键不依赖重连、配置revision或进程。已提交撤权、编辑/删除等取消待发；领取后发送前再次检查。发送后撤权不伪造撤回，晚到回执只结算Delivery。

unknown永不回到ready。声明可查的通道只读查询，精确scope/幂等键/remote ID匹配后才结算；not_found不证明未发生。无可靠查询能力明确等待人工，不能盲重发。已确认发送的remote ID、验证机器人身份和精确话题/安装组成来源因果链，正文标记不产生自身消息身份。

共享修改仅：Engine组合/验证/defer/回执隔离/恢复；API导入注册；FastPath确认自身结果hook。Dispatcher、财务/预算算法、Harness内核和历史迁移不改。共享范围以完整补丁hash为准，私有模块及正式集成检查在确认前继续准备。主库不应用迁移；受控测试使用独立PG。
