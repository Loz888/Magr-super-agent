# SAV1-06 固定本地工程候选

源码提交 15591aea214e7a83f3bf5ad112bda85b4294664e；源码 SHA-256：
2f47b76904cff3fb2e1c062258364e1a495a310150e4a759d42bfc16772ee100。本卡工程通过，等待独立复验；SA06仍IN_PROGRESS，不进入SA07。

原验收器 make verify TASK=SAV1-06 实跑 744 passed，
0 failure/error/skip；包含SA06新增178项及全部既有566项。make lint、
make typecheck（229 source files）通过；前后源码指纹一致，28项产物hash匹配。
真实注册结果：evidence/SAV1-06/20261111T082512Z-f4a54cb3/result.json。全部日志/JUnit引用见accept/SAV1-06/accept.json。

实现官方lark-oapi1.7.5长连接包装、默认禁用Webhook、安装/租户/群/发送者/结构化@/
显式话题范围、server-only Secrets、固定只读Broker、有界历史、健康/gap与登录设置。
成功ACK在唯一Engine持久提交后发送；旧连接输入/ACK、分片/限额、PG会话丢失/重投、
撤权和已知权限错误有真实PG及受控协议回归。实时过滤和历史unsupported变化提交最小
无正文失效；上下文机器人/仅context角色不升格请求，不伪造历史或最后编辑者。

0035已在本副本loopback:55435真实PG应用，35份安装校验和匹配。Engine、ToolAuthority、
预算、Harness及0001–0034原字节未变，旧GitHub Webhook AST未变。旧SA05升级测试仅
追加0035清单期望，原行及checksum断言不变；当前来源manifest前向更新Ingress哈希，
此前固定验收与原失败记录不改。失败索引：evidence/super-agent-v1/SAV1-06/20261111T064222Z-start/failure-index.json。

仅engineering_gate PASS；完整系统安全、真实来源/编码/发布、长期运行均NOT_RUN。
真实API、付费调用和对外业务写均0，无真实凭据登记、监听或外发。SDK故障覆盖真实PG
会话关闭/新Engine与受控ACK丢失，SDK特定跨进程SIGKILL/真实重推未验；原SA04实际
隔离PG暂停/SIGKILL按其原范围保留。历史OpenID/mentions表示不同则保守处理，不猜
身份、不宣称无限补齐；token不冒称平台只读scope衰减。生产Vault/轮转及后续卡未开放。
