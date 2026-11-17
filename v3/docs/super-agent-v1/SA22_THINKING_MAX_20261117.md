# SA22：启用思考模式与max推理强度

状态：**配置已生效，新模式真实推理待新的群消息**。2026-11-17。
用户明确要求“请打开思考模式”“并且设置推理强度为max”。

当前模型 `deepseek-flash`（官方版本DeepSeek-V4.1-Flash-1017）。实际账号只读模型列表
确认支持max；问答和编码的新请求均发送 `thinking: {type: enabled}` 与
`reasoning_effort: max`。Bot已于11:43:41 UTC（上海19:43:41）重启并在线加载配置。
不是只修改文档或仅更改编辑器的推理设置。

官方[思考协议](https://api-docs.deepseek.com/guides/thinking_mode/)要求带tools时完整回传
先前assistant的reasoning_content；现从同一attempt原加密、鉴权、TTL响应记录恢复，
不使用跨任务缓存，不将思考内容发到群里或存入普通日志。未具备匹配记录时发送前拒绝。
未修改共享Harness契约、Engine/API、schema或原财务算法。

新增注册 `SAV1-22-THINKING-MAX`：**4 PASS / exit0**，候选
`3c18581d1dcfa6847d91b6cb77e9ec489de105fa`，指纹
`f44031a4d67bfc8f4728e9c247ce106957291b3129abb3ed800bb2387ff72a5c`前后一致。
原通过项不重跑。验证问答参数与完整输出Token计费、实际工具多轮思考回传及加密隔离、
缺思考历史不发送、原Engine中可证本地拒绝为已知零费用。受控模型通过不冒充新模式真实质量通过。

[官方峰值价格](https://api-docs.deepseek.com/quick_start/pricing/)与既有保守缓存未命中报价相容。
所有输出Token包含思考，按供应商完整completion_tokens归原账本。共享累计USD10、
每Coding Run USD0.30／8模型／24工具／180秒、执行及外发范围保持；输出上界问答4096、
Coding8192，不将max强度解释为无限Token。Coding输入字节65536按同一上界预占，
8次最坏模型预占USD0.235936，仍低于原Run硬额。

切换新增费用0。原40条usage与9份trial完整行hash一致，已结算USD0.048930，旧unknown
预占USD0.019661完整保留，没有重发／退款或续跑L01。原模型及case许可文件不覆写，
另存受信模式manifest并绑定源码hash；新消息才采用新模式。

模式manifest SHA256：`b09a222b433c7d87299cb49b510b5fe8b2e2864c4b8f5aab9b3addb56bd7ae6c`。
证据位于本轮 `thinking-max/`，含新注册JUnit、官方能力查询、请求约定、源码归档、
私有控制代码hash、切换前后行hash及在线状态；不包含密钥或私有grader代码。
主库44／接入52不变，独立72h进程未触碰。完整SA22尚未通过，H12原FAIL及发布关闭保留。
