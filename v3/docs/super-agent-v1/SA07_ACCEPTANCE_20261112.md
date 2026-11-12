# SAV1-07 工程交付，等待独立验收

状态：IN_PROGRESS / ENGINEERING_COMPLETE_PENDING_INDEPENDENT_REVIEW。固定实现 `6fd95819883ed923d963e5d89858917a2fa3eab8`，源码指纹 `2d90679ae5310a8b12db4d4bc508f3521fb122ea3ac507df48d074291f19f2e7`。

共享组织/项目预算、Run子额度、原账本费用/Token归集、签署价格版本和配额已实现。覆盖 Engine、Router、RepositoryAnalysisStore 三条原预占入口，继续使用 Engine 同一持锁 PG 连接；原结算入口 AST 未改，不建立第二份实际费用账本。

`make verify TASK=SAV1-07` 单轮完整注册 **1118 PASS**：本卡 223 项、既有回归 895 项，0 failure/error/skip，exit0；前后源码指纹一致。`make lint`、`make typecheck`（234 files）PASS。结果、命令、JUnit、完整日志见 [正式摘要](accept/SAV1-07/accept.json)，原始注册结果 `evidence/SAV1-07/20261112T053016Z-22fb6856/result.json`。

## 要求与验证

| 要求 | 主要证据 |
| --- | --- |
| 三入口共享总额、组织/项目/Run及Router子上限 | test_super_agent_sa07.py 的共享入口、各限额、价格、并发、事务回滚用例 |
| unknown跨模式/新Run/午夜、晚到结算、缺Token回执 | 同文件的跨窗/DST/revision/recovery，及 test_super_agent_sa07_repair.py |
| 候选/来源/通知窗口、queue/slot持久占位 | 同文件配额幂等/并发/不可退款/明确释放；test_super_agent_sa07_hooks.py 验证实际SourceGateway/Outbox入口（受控来源、真实PG） |
| 签署版本、Cookie/Origin/CSRF与当前session | test_super_agent_sa07_api.py、test_super_agent_sa07_api_regression.py |
| 0035→0036保留旧Worker/Router账本及DDL回滚 | test_super_agent_sa07_migration.py；旧35迁移字节一致，独立本地DB36份校验和全部匹配 |
| 严格金额/Token/价格与时区/DST | test_super_agent_sa07_domain.py；数字字符串时间解析修复前失败及修复后证据保留 |
| 旧来源、Engine/API、Router、仓库分析与恢复 | 原SA06完整注册保留，并加入原Router和仓库分析回归 |

本轮修复异常usage JSON导致视图崩溃、旧repo Token预占被忽略、数字字符串被当epoch、旧issue_comment绕过通知配额四个问题。共享接线变化涉及的当前交付清单已同步指纹，原清单先行归档，旧契约断言未改。初轮失败、测试布置错误、会话重启中断、固定提交前主动中断及原1113PASS/5SKIP均保留；5项隔离PG环境定向重跑不与原1113相加，不拼接为最终成绩。最终成绩只引用上述固定候选单轮注册结果。

## 操作和复现

从仓库根执行 `make migrate`、`make lint`、`make typecheck`、`make verify TASK=SAV1-07`。本副本使用 loopback PostgreSQL 55435、独立 Compose/临时目录；旧恢复检查需已准备的本副本 R5 沙箱。`0036` 已安装，原35迁移不改；前向迁移不自动回退或删除历史账本。停止新预算使用管理员签署 disabled 新版本，unknown与历史费用继续占款。

旧仓库数据库检查显式要求进程环境中的 `MGAR_V3_DATABASE_URL`，仅由 Settings 读取 .env 不满足该旧开关。最终完整注册使用本副本同一DSN显式导出，全部用例仍由fixture建立独立测试库。复现时不输出连接值：

```bash
cd v3
.venv/bin/python - <<'PY'
import os
import subprocess
from mgar_v3.settings import Settings
env = dict(os.environ, MGAR_V3_DATABASE_URL=Settings().database_url.get_secret_value())
subprocess.run(["make", "-C", "..", "verify", "TASK=SAV1-07"], env=env, check=True)
PY
```

管理接口和字段见 [预算契约](SA07_BUDGETS.md)，接口不提供调用、结算、配额消费或付款端点。额度配置不激活模型/来源/外发，未配置新预算时旧入口仍受原grant约束，新Source模型权限没有共享预算时拒绝。

## 交接边界

工程门 PASS；独立验收待进行，整卡保持 IN_PROGRESS，SA08不启动。完整系统安全、真实来源、真实编码、发布与长期门均 NOT_RUN；本轮真实平台/付费/外部业务写0。Harness固定cb422c5第30节已核对，0036归Codex；0037及PersistentBudgetLedger的后续集成另验。实际Scheduler/Worker/Recall/多渠道发送不由本卡配额端口推定完成。
