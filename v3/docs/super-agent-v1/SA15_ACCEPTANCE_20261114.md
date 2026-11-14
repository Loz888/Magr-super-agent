# SA15 本地受控工程交付（2026-11-14）

SA15 本地受控工程已完成，等待独立验收；整卡保持 IN_PROGRESS，SA16未启动。
源码候选 **bc1f91c8f5fc8acaf3bb80e1649c4718c0d36a79**，源码指纹：
`13680b9009f8e33fd57c4e414c71bfcd73c8f337c804127ce6e7e546d1f8acbc`。

Harness `e0432f0e289de868a5956339aeec76db71bd0efe` 第40节已只读核对，Engine/API
两文件按精确补丁落地，9个只读接口可经原应用访问；原44份迁移不变，未预占0048。
未导入H10新文件库或H12活动实现；全部业务事实仍由原Engine持锁连接管理。

## 分版本成绩

| 候选 | 范围 | 成绩 |
| --- | --- | --- |
| 5a172ff | 私有契约/planner/加密游标准备 | 89 PASS；不重跑 |
| 1f4c332 | 首次正式PG/API/工具/质量检查 | 94 PASS、3 FAIL |
| 0fc4ce1 | 仅原3失败定向复验 | 3 PASS |
| 0fc4ce1 | 尚未执行的关系/Memory/Job/ACL首次检查 | 9 PASS、1 FAIL |
| 69ad502 | 仅原产物引用失败守卫 | 1 PASS |
| 69ad502 | 私密候选解密前过滤首次检查 | 1 PASS |
| bc1f91c | 正文TTL后当前任务状态与正文分离首次检查 | 2 PASS |

**110个正式独特目标、89个准备目标均有分版本通过记录，剩余失败0。**
没有重复已通过的测试目标；不宣称最后候选单轮全量110/199 PASS。原失败与日志完整保留。
[逐例版本映射](accept/SAV1-15/cases.json)／[机器摘要](accept/SAV1-15/accept.json)。

首轮三失败分别为：异步任务尚无checkpoint时误走非异步鉴权分支；测试构造空roles违反原契约；
第二安装测试复用了同一真实安装标识。修复后只复验对应三项。新增关系轮的唯一失败是上层
catalogue仍引用下层旧hash，已按依赖顺序刷新，仅该守卫复验。源码功能没有通过削弱断言放行。

首轮静态错误保留；原新源码/核心测试lint目标与5文件typecheck在0fc4ce1通过。新增relations
文件lint的两处格式错误仅该目标于69ad502复验通过；predecrypt及status-retention新文件lint
分别首次通过。随后私有鉴权/状态改进只执行新增定向功能检查；按用户规则未重复已经通过的
静态目标。因此静态PASS明确绑定其记录版本，不能称为bc1f91c全量lint/typecheck复跑PASS。

## 实际覆盖

- 原Source/input、Thread、Ticket、Run、真实受控异步Job、Evidence/Artifact、两代Memory、
  Action/Outbox与ContextEdge关系；默认无正文、真实当前状态、unknown不伪称完成。
- 当前Owner cookie/Origin-CSRF、独立Worker bearer/Run/Assignment/Session/epoch；同名群、
  安装、仓库、原目标与跨任务隔离，ContextEdge不授予读取权。
- 已授权同会话的其他Task可查；私密候选在解密前过滤。来源正文到期后，当前仍有权的已知
  Task元数据可读，过期Source及派生产物正文继续拒绝。
- 逐跳鉴权、节点/边/深度/字节/扫描/时间上界；UTF-8分页、opaque cursor作用域/篡改/到期；
  来源编辑/删除、撤权、登出、epoch与内容版本变化失效。
- 实际CAS IO在SQL事务外；IO期间提交的撤权、状态版本变更、Memory撤销和Knowledge ACL
  撤销在释放正文前阻止旧内容。派生内容检查全部已知来源，摘要无新事实或授权能力。
- 检索前后PG全表快照不变；显式工具只读、不创建新Worker、模型、通知或业务副作用。

冻结12例合成词面/时段检索均有首轮PASS，含真实任务ID/状态、歧义澄清、未命中与时段排除。
[逐例质量记录](accept/SAV1-15/quality.json)。这是固定合成工程分母，不是自然语言日期解析、
付费模型语义能力或真实团队检索质量。源码与命令以各轮不可变Git archive及指纹绑定。

## 完整性、资源与边界

44份原迁移校验和一致，443个当前catalogue产物引用匹配；Engine/API after hash仍与第40节
精确确认相同。4轮临时PG均已删除本轮数据库和会话，4个PG容器与2个实际受控Job容器无残留。
用户未跟踪文件的原SHA256不变、未暂存；历史失败未删。[完整性记录](accept/SAV1-15/artifact-audit.json)。

固定H10仅继承其已审计F01/F02范围，其他OPEN保留；H12真实Agent/MVP固定FAIL由另一CLI负责。
真实平台、付费、外发、发布、全系统安全与长期运行均未在本轮执行；相关门保持NOT_RUN。
本卡只读工具为显式服务端ToolContext adapter，不默认接入模型或修改固定执行器。
独立验收未执行，不自行标DONE或进入SA16。具体接口与限制见[Recall说明](SA15_RECALL.md)。
