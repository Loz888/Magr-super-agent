# SAV1-04：持久入站、幂等、编辑与缺口

契约 `source-ingress-v1`；整卡结果见 `accept/SAV1-04/accept.json`。本卡提供受信 Gateway 端口、同一 Engine/PG 持久收件箱、加密正文、游标/gap、重放维护工具。不启动平台适配器、监听、模型、Worker 或发送。本卡的业务输入是唯一 `source_inbox` + `source_ingress_inputs` 版本事实，不创建假 Issue、Thread/Ticket/Run；实际候选路由和来源任务创建仍按 SA08–SA13 的依赖验收。

## 受信入口与 ACK

服务端明确注入 `(source_kind, adapter_version) -> IngressVerifier`，缺 verifier 拒绝。verifier 必须验签/安装证明及原始租户、安装、真实作者、事件类型、版本和正文 hash；显示名、引用、正文和模型不提供身份。`VerifiedDelivery` 是内存端口，不是 HTTP 请求 DTO。当前没有生产 verifier；合成 HMAC 测试不能证明 GitHub/飞书平台已接入。

Gateway 在 SQL 事务外调用 verifier；Engine 提交时重查安装配置、当前 ownership generation、签署策略、可信已绑定身份及当前上下文角色。沿用 SA02 的大小写敏感身份，不放宽其严格 proof replay API，也不通过 bearer 扩权。

Engine 持有原 advisory lock、原 PG 连接和原互斥锁。入站要求最外层事务，拒绝调用方外层 savepoint，`synchronous_commit=on`，持久写入 finalized receipt/版本/游标后才返回，Gateway 仅在连接 IDLE 时调用 ACK。ACK 失败/回执丢失可重交同一原始事件，复用原回执，不重新产生业务输入。连接失效不另开业务写连接绕过锁；重启须重新取得唯一 Engine 锁。

SDK 手动 ACK 或 handler 返回后 ACK 的组合模式可接线；before-handler ACK 模式拒绝启动。测试复现“提前 ACK 后回调丢失，PG 无回执”的受控 SDK 边界并记录不可恢复 gap。实际 SDK 内部 ACK 顺序、平台重放保证和断线语义由 SA05/06 实测；本卡不承诺平台 exactly-once、无限补拉或物理存储掉电恢复。

## 去重、冲突和版本

- 传输键：完整 SourceScope（平台、内部 SourceInstance、安装、租户）+ delivery_id。
- 事件键：完整 scope + event_kind + remote_event_id。
- 实体键：scope + 受信实体命名空间 + entity_id；版本键使用明确的 native/derived revision，无法提供 revision 时保留 event_key，不臆造原生顺序。
- 同一 delivery 的原始 payload SHA 或稳定规范化指纹变化，记录 `INGRESS_DELIVERY_CONFLICT` 并拒绝 ACK。相同事件/实体版本/原生序号的矛盾也有独立冲突回执，旧正文不被冲突覆盖。
- 不同 delivery/remote event 指向同一合法版本时，仅增加传输回执/别名，返回原 canonical input ID。数据库唯一约束、唯一 Engine 事务处理并发。

高版本编辑使旧正文和旧输入不可读；晚到低版本保留最小事实、标为 unavailable/context_only，不覆盖当前版本。缺原生排序的不同版本标为 ambiguous 并使正文失效，不按到达时间猜最新。撤回保留不可逆 tombstone，清理所有相关正文；晚到 create/edit 不复活。只有真实作者或受信平台 moderation 事实能失效原实体。已验证作者的编辑/删除即使当前 Receive 不允许保留新正文，仍清理已有正文；拒收事件不保存新内容。

数据库保护版本/权限快照、sealed receipt、消费事实和 tombstone 的不可逆性；历史迁移 0001–0032 原样。新事实只在 0033 中定义，关联已有 Source/identity/policy/inbox，不增加第二业务状态机。

## 游标、时效、容量与补拉

每个来源 stream 明确 epoch，独立记录连续 committed sequence 与最高 observed sequence。1、3 只提交水位 1，记录缺失 2；只有真实持久接收 2 后才能关闭有界序号 gap 并推进至 3。epoch 变化和人工 CAS reset 保留未知 gap，reset 不冒充恢复完成。同序号矛盾明确拒绝。

默认 300 秒 live age，未来超过 30 秒或晚到事件、gap 期间、历史/缓冲重放、disabled/shadow 都只能作为 context_only。本卡不触发任务。每来源默认最多 256 个可用待消费输入、10000 个接收回执、64 KiB 正文、4 MiB 留存密文；上限显式可配且有硬界。容量不足不 ACK，不静默丢弃；有界最小冲突/gap 账本耗尽也拒绝继续接收。删除清理可在积压已满时先落实。

补拉前检查当前 policy/source revision、时间、Receive 历史开关、会话/话题和当前允许身份角色，再在事务外调用明确的 HistoryProvider；页面最多 100 个事件并受积压余量约束。页面跨会话在提交前整体拒绝，提交时逐项重查当前权限。所有历史默认 context_only，不批量开工。缺历史能力、断线、权限不足、SDK 提前 ACK、不可回放区间记录 gap；不伪造不存在的消息或补齐结果。多投递页面以逐回执提交为单位，部分成功后可幂等重放，并非声称跨平台页面原子性。

## 正文、保留与缓冲

不在 Receive 范围的正文仅在验签/确定性检查的有界内存中存在，不进入 PG 正文/普通日志/模型/Recall/CAS/传输缓冲。拒绝回执仅保存无正文 hash、计数和固定 reason code，不保存原始 headers、令牌、显示名或正文。Receive 允许但 `retain_body=false` 时仍不保存正文。模型权限、执行权限、回复与发布独立，本卡没有任何模型入口。

获准正文以 Fernet authenticated encryption 独立存储，并绑定 canonical input ID。正文读取需当前控制台会话、当前 owner/Receive/身份与版本、实体未删除/歧义/过时，且不超过有效期。TTL 取策略 TTL、原发生时间 TTL、metadata TTL 和策略期限的交集。策略/角色/来源/owner 变化通过同 PG trigger 清理密文并不可逆失效旧输入，后续新授权不复活旧内容。普通管理视图不返回正文、密文或 canonical sender payload。

显式 purge 清理到期密文、过期输入 metadata/sender、无业务引用的旧 source_inbox 和回执原始 delivery ID，保留 hashed 去重索引、tombstone 和最小审计。关联既有业务的最小引用独立保留。最小 hash/审计仍属受控运维数据，回执容量是有界的；耗尽需操作者审查归档/更换来源范围，不能通过删除去重事实制造再次开工。没有启用后台清理服务，SA20 常驻维护和 SA21 备份/Memory/Recall/导出/外发全链删除仍须另验。

可选 `EncryptedSpool` 仅接收 Engine 在当前 Receive 保留权限下签发的短期 BufferPermit，默认 30 秒、最多 300 秒，同时不得超过正文 TTL。若数据库在新鲜预鉴权前不可用，不缓存正文、不 ACK；预鉴权后断线才可加密写入重交日志，缓存成功也不 ACK。日志没有任务裁决、费用或授权写入能力，只能重新提交唯一 Engine。重启重交需重查当前 generation/身份 revision/Receive，过期或撤权丢弃并记录 gap；重交为 context_only。

缓冲和密钥只允许本副本 `.local`，目录 0700、文件 0600、所有者匹配、regular file/单硬链接；逐层 openat/NOFOLLOW，不跟随符号链接。文件数/总字节限额、flock、fsync、AEAD 绑定及损坏拒绝均有负例。没有默认内容密钥或生产缓冲目录；运维显式供应 `BodyCipher.from_private_file` 并注入 Gateway。密钥保管/轮换/恢复和到期清扫须与服务部署配置一起验收；不把加密缓冲当作无损无限传输或备用业务数据库。

## 管理、重放和旧入口

`/api/v3/source-ingress/{source_id}` PUT 保存 expected_generation/settings/review_ref；GET `/view/{receipts|inputs|gaps|cursors|conflicts|audit}` 返回本项目最多 100 行；POST `/replay`、`/purge`、`/cursor/{stream_id}/reset`。全部要求当前唯一主人 cookie，写要求受信 Origin/CSRF，响应 no-store。没有公开 raw ingress、body、bearer 或身份断言端点。保留服务端 body 方法供后续受鉴权接线，本卡无模型/Recall 接线。

`python -m mgar_v3.sources.ingress_cli --session-file .local/<private-dir>/session.json --source <uuid> receipts` 查看；`replay --generation <n> --input <uuid> --review-ref <ref>` 有界重验；`purge` 清理。会话文件只含 cookie/csrf_token、0600，父目录 0700；客户端仅允许显式环回 HTTP origin，不跟随重定向、不打印会话。它调用运行中的唯一 Engine 管理 API，不另开数据库写者，也不自行启动服务。Replay 只重验持久输入并保持 context_only，不重置 consumed、不新建 input/Task，不承诺尚未实现的 Router 消费重放。

未配置的旧 GitHub/CLI/Web 原样。GitHub 显式映射本项目 repository 后，owner=gateway 阻止旧入口，owner=legacy 阻止 Gateway，owner=disabled 阻止双方；generation CAS 切换使旧 Gateway 立即失效。旧 Engine 已接收的同 delivery 由新入口返回 legacy_owned/原 Trigger，不再产生 Source input；正文不符报冲突。已经由新入口拥有的 delivery 不能切回旧入口重造任务。旧 schema 尚未迁移时只读 guard 直接兼容返回。

## 协作、复现与限制

SA01/SA03 依赖及 SA02 契约完整阅读记录、UTC/SHA、独立结果和已确认交接见本卡 start evidence。Trae 固定 `a04dbdeb48669add157cc3d88021ea6563b0f160` §17 确认 0033、sources/API 与 Engine.__init__；`bd508300cf99e276b243a2d9c3d5ebc171ca4c08` §19 确认 Engine.ingest 一行只读 owner guard。未编辑/导入 Harness 活动树，没有扩大 ToolAuthority/预算/模型权限。

根目录运行 `make lint`、`make typecheck`、`make verify TASK=SAV1-04`。注册包括 SA04 真实 PG 并发/连接中断、独立进程 SIGKILL 四点、专属临时 Docker PG pause/unpause、32→33 升级/checksum/旧行，以及完整 SA03 注册回归。暂停对象用唯一 UUID/ownership label 核对，仅环回专属端口，不操作本副本主 PG 或 Harness PG；测试镜像固定且 `--pull=never`。旧 R5 恢复回归需要本副本已有 R5 sandbox-lock（见 SA03 文档），不能用删用例或跳过代替。

保留所有 FAIL 和 NOT_RUN。真实平台 SDK、完整系统安全、模型、编码、外发、发布与长期运行均未由本卡放行。停止在 SA04 等待独立验收，不自动启动 SA05。


## 独立复验 P1/P2 修复（2026-11-11）

原 2247db3/ad4aae8 的 374 项注册全绿不构成独立放行；独立新增两项 PG 回归失败。修复前原样复现为 2 failed/3 passed，原评分器保持不变，原验收存档在 accept/SAV1-04/history/2247db3-accept.json。修复结果另见 accept/SAV1-04/repair-20261111.json，最终放行仍等待独立复验。

现有 source_ingress_entities.native_order 表示已可信观测的最高原生版本水位，可高于 head_input_id 对应的已接收版本。拒收编辑只推进最小 hash/水位事实并失效旧内容；首次观测即被拒收时也保存此最小事实，不保存 body、body hash、完整事件或新业务输入。新事实只能在有界接收凭证内创建；已有实体的隐私失效仍可在积压满时执行。观察事实与回执共用原 Engine 事务并一起回滚。

低于观测水位的版本不可读；等于水位但没有既有 canonical 版本证明的重投同样保持 unavailable，不因恢复角色或历史/重放而复活正文。当前权限允许的真正更高原生版本可以形成新的当前输入。拒收且没有可信原生顺序的不同编辑留下不可逆 ambiguous，之后保守拒绝把任何不确定版本当新内容；不能通过恢复角色清除此不确定性。本卡不提供自动解除歧义的入口。

新编辑/删除失效之前，先使用已经通过冲突检查的事件/版本索引识别 canonical 等价投递。等价无序编辑换 delivery/remote event 或并发重投仅复用输入，不删除当前正文；真正不同的无序版本继续按歧义处理。原生排序、删除 tombstone、当前身份/Receive、保留期限与不可复活约束不放宽。

本轮仅修复私有 SourceIngress 逻辑与正式回归；Engine、API、DTO/schema、迁移 0001–0033、ToolAuthority、预算和 Harness 均不变，不申请或占用新迁移。H04 另有只读材料核对，记录在 docs/material-reviews/，不属于本卡工程门，不授予 H04/H05 放行。

拒收的带版本 create/历史快照也遵守同一观测水位与无序歧义规则；不能依赖 operation 名称忽略已知新版本。新增回归覆盖该形式及晚到 create 不得清除较新 canonical 正文。中间候选 3eca833 的 390 项 PASS 和快照邻接 1 FAIL 均保留，最终结果另行验证。
