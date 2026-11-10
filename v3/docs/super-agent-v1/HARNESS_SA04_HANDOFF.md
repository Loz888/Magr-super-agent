# Codex → Trae：SAV1-04 固定交付

实施 `ad4aae84bdc61750626eeaefc17eabf9d28c4a65`；source-ingress-v1；注册 374 PASS（本卡 70，0 failure/error/skip）、lint/typecheck 215 files PASS，源码 SHA `e809eea055eb42424aab309bef2d000cb7f4f05e41e68e7421ee1aec458cc99c` 前后一致。结果 `evidence/SAV1-04/20261110T181604Z-62d17c77/result.json`。

按你实际确认 a04dbde §17、bd50830 §19 实施 0033_source_ingress.sql、sources Gateway/Ingress/加密缓冲、source-ingress 管理 API；Engine 仅 SourceIngress import/init 和 ingest 一行只读 owner guard，API 仅 import/register。其余 Engine 方法、ToolAuthority、财务、旧迁移 0001–0032、SA01/02/03 产物和 Harness 保持原样；未导入你的活动树。

入站持久最外层 commit 后 ACK；传输/事件/实体/编辑版本分别去重，矛盾有冲突回执。当前 Receive/身份/角色控制加密 TTL 正文，编辑/撤回/撤权不可复活；游标缺口仅实际回执覆盖才能闭合。缓冲需新鲜预鉴权且仅重交 Engine；补拉重验当前权限、历史 context_only。无仓库输入保留既有 source_inbox，不造 Issue/Task；后续 Router/Worker/Recall 按原 DAG。

真实独立进程四 SIGKILL、专属临时 PG 暂停、连接终止和 32→33 升级均在注册用例中通过；SDK 是受控提前/延后 ACK 边界演示，实际平台 SDK 仍 SA05/06 NOT_RUN。完整安全/付费/编码/通知/发布/长期门仍未放行，本轮付费和外部业务写 0。

详见 SA04_INGRESS.md、config/source-ingress-v1.json、accept/SAV1-04/accept.json。所有历史失败保留；本卡等待用户独立复验，不自动进入 SA05。下一 0034 和任何 Engine/API/预算重叠仍需另行实际确认；此文件不表示消息已送达或 peer 已验收。
