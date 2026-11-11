# 本地 Codex CLI 接手任务卡：SAV1-06

任务 ID：SAV1-06-CLI-TAKEOVER（交接子任务，不是 SA07）。
编写日期：2026-11-11（Asia/Shanghai）。
接手目的：由本地 Codex CLI 继续负责 Super-Agent；保留当前 SA06 独立未通过状态，
复核剩余问题并在已授权范围内修复具体缺陷，完成后等待新的独立验收。

## 1. 工作范围和禁止越界

- 唯一工作树：/home/imini/magr-super-agent；应用目录为其 v3/。
- Harness 由本地 Trae 负责，活动树 /home/imini/magr-harness；不编辑、导入其活动文件，
  不运行或推进 H 卡。当前 SA06 没有需要跟进 H 卡的前置。
- 复用原唯一 Engine、持锁 PG 连接与事务。不新建业务数据库/第二业务状态机。
- 0035_feishu_source.sql 已归 Codex 并应用；0036 未预占。共享 Engine/API/schema/迁移/
  预算或 ToolAuthority 重叠前，先取得并记录实际所有权确认。
  历史确认是 Harness 固定 d0053f4d7bdf850f4c3c458bd36d3c10b9a3e650 第29节；
  不是新共享改动的通行授权。消息“等待发送”不等于送达或确认。
- 不启用真实飞书/GitHub监听，不登记实际凭据，不做真实API/付费模型/外发/发布。
  完整系统安全、真实渠道、编码、发布和长期运行门仍 NOT_RUN。
- 不修改独立原探针、评分器或阈值强求通过；保留所有 FAIL/NOT_RUN 和原始日志。
  不重置/丢弃用户修改，不覆盖活动检查，不用 SQLite 代替真实 PG。
- SA06 仍 IN_PROGRESS / NOT_PASSED；SA07 及以后卡不启动。
  接手实现者的复核不自动等于新的独立放行，须等待用户转达新的独立验收结论。
- 不执行此前被拒绝的操作或换工具绕过否决。遇确认/拒绝/具体阻塞，说明行动、
  目标、原因和是否已执行；开发授权不代替安全确认。

## 2. 固定接手基线

| 项目 | 值 |
| --- | --- |
| 交接前 HEAD | 3c7b7009cbbc985c07106bd304532f67e9dda079 |
| 最新实施提交 | d8004baadbf8e0afff1fc9e2dbbcd82ee2cf93ba |
| 代码/测试/配置指纹 | 4738e6421c3e4648e269e3720aec18c42cb5ad93528501eba5bf00f07a91a863 |
| 原被审 HEAD | 37490134951c62f38efe614cb7749a3c505466fd |
| 原源码候选 | 15591aea214e7a83f3bf5ad112bda85b4294664e |
| 原源码指纹 | 2f47b76904cff3fb2e1c062258364e1a495a310150e4a759d42bfc16772ee100 |
| 交接前工作树 | 干净；本次只新增任务卡文档，未推送 |

这张卡本身可以有一个后续文档提交；不要把仅增加本卡的 HEAD 差异当成源码漂移。
如 HEAD/指纹或工作树有其他变化，先审阅差异和用户修改归属，不硬 reset 回表中提交。
以实际文件/当前 Git 为准，不把本卡摘要当作验收证据。

## 3. 接手第一步：完整重读、记录与检查活动进程

先完成以下动作，再改源码或开始会修改 PG 的验证：

1. 完整阅读 /home/imini/magr-super-agent/AGENTS.md、
   /home/imini/magr-super-agent/v3/AGENTS.md、
   /home/imini/magr-super-agent/v31-upgrade/TASK_QUEUE.md。
2. 完整阅读 /home/imini/magr-super-agent/v3/docs/super-agent-v1/TASKS.md，
   包含 SA06 全卡及引用要求；同时阅读 ARCHITECTURE.md、ACCEPTANCE.md、COORDINATION.md、
   SA02_IDENTITIES.md、SA04_INGRESS.md、SA06_FEISHU.md、SA06_REPAIR_20261111.md。
   上述短文件名均位于同一个 docs/super-agent-v1/ 目录。沿引用继续完整读取适用要求，不能只读本卡摘要。
3. 阅读 /home/imini/magr-super-agent/v3/docs/adr/0001-v31-boundaries.md，
   以及 /home/imini/magr-super-agent/v3/docs/super-agent-v1/adr/ 中适用的
   SAV1-00/02/03/04/05/06 ADR。核对 SA02、SA04 依赖与此前独立放行记录。
4. 完整阅读第4节原始反馈六文件、最终修复验收 JSON 和当前实现/测试。
   保存 UTC 阅读时间、每份文件 SHA-256、当前 HEAD/源码指纹、工作树状态。
5. 只读确认是否仍有独立复验、pytest、verify.py 或 probe 在运行。
   有活动检查时不改被审源码；先确认该次检查结束并保留结果。
   旧独立完整复验在 2026-11-11T09:19:03Z 已结束，新接手仍须核对当前活动状态。

在 v3/evidence/super-agent-v1/SAV1-06/ 下创建新的 UTC+唯一后缀 CLI 接手目录。
不得复用此前失败目录或覆盖旧读取/验收记录。上下文重置或任务书变化后重新阅读。

## 4. 必须读取的原始反馈和证据

以下路径均相对 /home/imini/magr-super-agent/v3：

原始反馈（全部完整读）：

- .local/sa06-independent-review/review-summary.json
- .local/sa06-independent-review/probe.py
- .local/sa06-independent-review/probe.log
- .local/sa06-independent-review/probe_formats.py
- .local/sa06-independent-review/probe-formats.log
- .local/sa06-independent-review/format-results.json

原独立终结记录：

- .local/sa06-independent-20261111T0945Z/final-review.json
- evidence/SAV1-06/20261111T085702Z-afe6983b/result.json
- 原完整复验为 744 项、10 FAIL（TMPDIR 错误）；定向9+1通过不能拼成完整绿。

当前修复交付：

- docs/super-agent-v1/accept/SAV1-06/repair-20261111.json（完整机器摘要）
- docs/super-agent-v1/accept/SAV1-06/accept.json（当前状态指针，仍未通过）
- docs/super-agent-v1/accept/SAV1-06/history/3749013-accept.json（旧候选历史）
- evidence/SAV1-06/20261111T100727Z-ed7d40c7/result.json
- evidence/SAV1-06/20261111T100727Z-ed7d40c7/junit.xml
- evidence/SAV1-06/20261111T100727Z-ed7d40c7/command.log
- evidence/super-agent-v1/SAV1-06/20261111T100727Z-repair-registered/pg-cleanup.json
- evidence/super-agent-v1/SAV1-06/20261111T100510Z-repair-static/commands.json
- evidence/super-agent-v1/SAV1-06/20261111T100528Z-repair-probe/probe.log
- evidence/super-agent-v1/SAV1-06/20261111T100633Z-repair-formats/format-results.json

原失败完整快照与阅读/完整性记录：

- evidence/super-agent-v1/SAV1-06/20261111T094358Z-independent-repair-start/
  内含 original-review/、original-full-review/、original-registered-fail/、
  reading.json、original-sha256.json、preflight.json、scope-audit-final.json、
  grader-integrity.json、repair-freeze.json、delivery.json。

.local/sa06-repair/ 下有之前的私有运行脚本及 state.json，可阅读参考，
但不是评分器，也不是最新用户授权或可以盲执行的状态生成器。
不要运行旧 finalize 脚本覆盖当前状态或把旧计数当成新成绩。

## 5. 已实施内容与准确结果

三类原独立问题：

1. P1：话题限定授权却 GET 整群。
   当前 native_thread_id 只来自真实 thread_id；root_id 仅回复关联。
   已提交、可信入站可在原 Feishu audit 留下最小 hash/receipt/metadata TTL 证明。
   证明不是授权；mint/GET 前仍重验当前身份、Receive、配置及范围。
   证明不足时网络前拒绝+gap；有效话题使用 thread 容器。
   游标有 Broker 私有 MAC 绑定 scope/config，重启失效；chat 耗尽不能声称覆盖话题回复。
2. P2：SDK 清理把新鲜撤权诊断覆盖成 unknown。
   当前连接观察独立记录，负面权限保留原原因/观察时间、不续命；
   自动断开和显式 close、DB/Runtime/真实登录管理 GET 已有回归。
   新配置、错模式、过期/未来、旧 granted/online 不能继承。
3. P2：畸形响应绕过固定错误/健康/gap。
   当前有界严格解码器、mint/probe/history 三阶段固定诊断、
   不自动重试/释放数据、不因格式错误推断撤权、不覆盖更强新鲜权限诊断。

当前已有结果（只代表这些实际运行）：

- 完整注册：795 PASS（SA06原178＋新增51＋既有566），0失败/错误/跳过，退出0。
- SA06专项：229 PASS；lint退出0；typecheck退出0（231 source files）。
- 33项交付哈希一致；注册前后源码指纹相同。
- 原格式矩阵：9 PASS、0 FAIL，退出0。
- 原四项 probe.py：3 PASS、1 FAIL，退出1，不能称全绿。
- 本副本 PG 的新残留测试库/会话为0；这是原 fixture 清理后的盘点，不是人工清除别人的库。
- 没有新迁移；既有35份迁移、已有schema、Engine/API与冻结Harness文件未改。
- 评分器字节、原注册目标和2700秒阈值保持；仅添加三份真实修复回归。

## 6. 下一步具体工作：复核剩余话题探针契约

不要先写计划摘要或直接开 SA07，先完成接手核对和以下受控复核：

A. 明确原 topic-only-remote-scope 为什么仍 FAIL。
它只在策略及 HistoryRequest 中写 Thread-Allowed，没有可信原生 thread_id 来源。
当前实现抛 FEISHU_HISTORY_THREAD_UNPROVEN，在 mint/GET 前拒绝并记 gap；
原探针未捕获这种“无法可靠表示时网络前拒绝”的允许结果，仍记该 case FAIL。
这是当前实现者的解释，需接手者读源代码和原要求独立判断，不能直接照抄成放行结论。
不得伪造证明、把 root_id 当原生话题、创建假请求记录或改原探针强求退出0。

B. 基于原需求准备新的、另存文件的受控接手复核，不改原探针。
最少区分两种互补条件，并保存远端请求轨迹：

- 没有来源证明：应固定拒绝、POST/GET次数0、持久gap、无正文/业务输入释放。
- 有签名/受信Socket真实 thread_id 且已持久提交、当前策略只允许该话题：
  应只请求 thread 容器和该 thread_id；覆盖回复、跨话题响应、续页/游标篡改、
  撤回及终止页完整性，并保留mint/GET中撤权负例。

检查现有 tests/test_super_agent_sa06_repair_topics.py 的证明是否真的来自受信提交，
而非测试直接写“成功”状态；同时核对下面源码，不把现有通过用例当独立正确性的证明。
补充复核属于接手复核，不冒充外部独立验收已经完成。

C. 有具体实现缺陷：先保留失败、补正式回归，再在SA06内最小修复。
无新缺陷：交付接手复核报告、说明原探针契约差异及限制，等待用户/独立复验方结论。
不要为改掉一个安全拒绝的 FAIL 而扩大权限；也不要只凭当前实现者解释认定问题已关闭。
没有源码变化时，不必机械重复已有长全量验收；如本轮承担新的完整复验或修改源码，
必须在固定新候选上重新执行完整注册检查，不拼接分轮成绩。

关键源码（均在 v3/src/mgar_v3/sources/）：
feishu.py、feishu_broker.py、feishu_topics.py、feishu_response.py、
feishu_runtime.py、feishu_store.py、feishu_sdk.py。
新增回归（均在 v3/tests/）：
test_super_agent_sa06_repair_topics.py、test_super_agent_sa06_repair_health.py、
test_super_agent_sa06_repair_formats.py。
其他原SA06/SA01–05回归和原评分器保持。

## 7. 环境与可复现命令

运行环境为本机 WSL Ubuntu-24.04，Linux bash；不要把 PowerShell 转义写进命令。
真实隔离测试使用本副本 loopback:55435 PostgreSQL，Redis为56381。
从现有服务端配置读取数据库连接，不输出 .env/口令/令牌；不要访问 Harness 的55433状态。
若依赖或沙箱锁不可用，记录 NOT_RUN/具体阻塞，不悄悄换SQLite、联网拉依赖或放宽沙箱。

接手环境设置与只读指纹核对：

~~~bash
cd /home/imini/magr-super-agent/v3
export UV_OFFLINE=1
export TMPDIR="$PWD/.local/tmp"
export PYTHONPATH="$PWD/src:$PWD/tests"
export PATH="$PWD/.venv/bin:/home/imini/.local/bin:$PATH"
export MGAR_V3_ALLOW_PAID=0
export MGAR_V3_ALLOW_EXTERNAL_WRITE=0
mkdir -p "$TMPDIR"
export CLI_EVIDENCE="$PWD/evidence/super-agent-v1/SAV1-06/$(date -u +%Y%m%dT%H%M%SZ)-cli-takeover-$(.venv/bin/python -c 'import uuid; print(uuid.uuid4().hex[:8])')"
mkdir -m 700 "$CLI_EVIDENCE"
git status --short
git rev-parse HEAD
.venv/bin/python - <<'PY'
from pathlib import Path
import sys
sys.path.insert(0, "scripts")
from verify import source_fingerprint
print(source_fingerprint(Path.cwd()))
PY
~~~

先确认旧检查结束，保存新执行目录并阅读命令实际副作用，再按需要运行专项或完整验收：

~~~bash
cd /home/imini/magr-super-agent/v3
.venv/bin/python -m pytest -q   tests/test_super_agent_sa06_repair_topics.py   tests/test_super_agent_sa06_repair_health.py   tests/test_super_agent_sa06_repair_formats.py   --junitxml="$CLI_EVIDENCE/repair-junit.xml"

cd /home/imini/magr-super-agent
make lint
make typecheck
make verify TASK=SAV1-06
~~~

CLI_EVIDENCE须先设置为自己新建的绝对证据目录；不能用空值或旧失败路径。
仓库根Makefile已固定 magr-super-agent Compose项目及本副本TMPDIR；优先从根执行make。
真实注册由原 scripts/verify.py 生成 result.json/JUnit，运行通常约20多分钟，2700秒阈值不变。
新检查中保持源码不变，并保存stdout/stderr/退出码/UTC/前后指纹及PG清理盘点。

若需要原样复跑用户探针，命令如下；分别保存退出码，不能用忽略错误伪称成功：

~~~bash
cd /home/imini/magr-super-agent/v3
.venv/bin/python .local/sa06-independent-review/probe.py
.venv/bin/python .local/sa06-independent-review/probe_formats.py
~~~

重要：probe_formats.py 自己会写固定路径 format-results.json；原始失败文件和测试产物
必须在执行前完整快照并记录SHA，把新输出另存后恢复原反馈文件，核对六文件SHA不变。
旧 .local/sa06-repair/run.py 提供过这种保存/恢复包装；先阅读再参考，不改原脚本。
同样保留原 probe.log/probe-formats.log，新的 stdout/stderr写自己的UTC目录。

## 8. 接手交付和停止条件

交付一份新的本地接手记录，至少包含：

- 实际HEAD/工作树、源码指纹、读取UTC/SHA与证据目录。
- 原剩余FAIL的根因判断、是否有新缺陷、实际操作及POST/GET/持久gap轨迹。
- 新旧结果分列：命令、退出码、JUnit、原探针精确结果；未运行项标NOT_RUN。
- 如有修复：固定本地commit、前后指纹、完整注册、lint/typecheck、PG清理证据及限制。
- 原始反馈/失败/评分器/阈值未改的完整性证明，确认用户修改保留。
- 当前SA06仍未独立放行；真实门NOT_RUN、SA07未开始。没有新的用户指令不推送/部署。

第一阶段完成后若没有新的具体缺陷，停止在SA06等待新的独立验收。
本任务卡不授权自行把安全拒绝改成远端读取，也不授权实现者自己跳过独立放行门。
