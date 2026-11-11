# ADR SAV1-05：安装身份、单一入站与最小只读凭据

状态：IMPLEMENTING；github-app-v1。继承 SAV1-00/02/03/04，保留旧 ADR0001/0003/0007/0008/0012 的权限和未知结果边界。

GitHub App 的共享 Webhook secret 经原 HMAC-SHA256/constant-time 分支验证；受信安装配置再校验安装、仓库不可变 ID、所属账户、可见性、事件及实际 sender ID/type。登录名与显示名不提供身份。GitHub numeric ID 使用原样十进制字符串，不进入旧 actor 小写化路径。

0034 仅增加安装配置/历史、显式只读 snapshot grants 与无正文审计。所有写经原 Engine 连接与锁。配置 CAS 还追加已有 SourceInstance 版本，复用 SA03/04 的 fencing 和正文失效；不复制业务状态机、不修改预算、ToolAuthority 或 Harness。

旧 Webhook 在所有事件分支之前按已配置的仓库 owner 分流，gateway 无受信 Runtime 时失败关闭；legacy 回切也检查该 delivery 的已有新管道回执。未配置旧入口继续原行为。原验签函数提取供双方共用，保留原 Issue 路径；新来源只产生原 source_inbox 输入，不提前创建任务。

事件包含 Issue/PR/review/comment/workflow/check；资源关系包含稳定仓库 ID、PR 列表、head/base SHA、head 仓库、workflow run/attempt 和 check IDs。关系与正文一起受 SA04 Receive/加密/TTL 读取约束。没有可靠原生序号的编辑使用 derived hash，保持 unknown-order 的保守失效，不能用接收顺序或 updated_at 冒充严格排序。

安装读取不等于 Worker 权限。只读 Credential Broker 为单仓库、精确 read permissions 签发短期安装 token，不缓存/公开 token，不交给项目进程。快照另需管理员签署、当前身份/角色/策略、固定 SHA/路径/时限/字节界限；私有源码的模型外发、运行、通知和发布仍需各自后续门。PR/fork/head 不匹配不得继承旧 grant。网络调用在 SQL 事务外，结果释放前再次核验授权。

默认没有 Runtime、没有 activation。设置保存、vault 引用或环境变量都不启动监听/令牌刷新/网络。受控 activation 只接受 httpx.MockTransport；真实 activation 是受信服务端组合输入，仍须单独冻结实际安装、仓库、数据与操作许可，不由本开发任务生成。无 Channel/发布写能力。

实际归属确认：Harness 固定提交 79798690c93497d1740d54cc91fc2f755f55f250 第24节。仅只读核对该交接，不导入或修改对方活动树，也不改变 H 系列验收状态。真实平台、完整系统安全、模型、编码、外发/发布与长期运行保持 NOT_RUN。

接收上界与隐私删除分离：受信、已选资源上的编辑/删除不能因 editor 没有 Receive 角色而保留旧正文。未选 editor 只经 SA04 最小事实/失效路径；Runtime 校验策略 actor 不得超过 App 白名单。令牌签发与数据 GET 是两个外部边界，后者重新检查当前快照/历史许可。

灰度切换补充：旧CI主动入口的原始 delivery 保存在 proactive_event_claims，原 Trigger 外部ID为 speculation:<uuid>，不能仅靠普通 Trigger 去重。新 Runtime 查询同项目/仓库/原始delivery的原claim，已由旧CI建工时明确拒绝再次建立 Source input。API owner预检和所有旧分支在原 Engine mutex 内线性化；不持有跨外部调用SQL事务。两项真实PG回归（既有claim回切、受控并发切换）修复前2 FAIL已保留。
