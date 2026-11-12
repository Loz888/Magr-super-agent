# SA09 本地工程交付记录

状态：IN_PROGRESS，本地工程交付PASS，等待独立验收（NOT_RUN）。工程结论使用首轮完整结果与原失败项定向修复证据；不是修复后一次全量PASS。
SA10未启动；真实平台、付费调用、真实编码、外发、发布、完整系统安全及长期门均NOT_RUN。
首次整卡冻结提交：`e9ab11e82920b62967568b967219d1b1fcc597ff`；冻结源码指纹：
`180c9b684c03b72239b464a5fe2245f9edca692d1fdc882859394154b4519417`。
首轮1777项为1776 PASS、1 FAIL、0 ERROR、0 SKIP，原FAIL保留。最终源码提交：`93e9a9d9a64bdd19889071ebf3ee33d3e46bb482`；
源码指纹：`684fe90a43065e642860ca3b99fbc932406951e92b3bd41392eb590e386436ce`。修复后仅原失败1项PASS，生产源码与首轮字节一致。

## 已实现范围

Source Eval复用原Engine同一持锁PG连接、Routing/RouterWorker分段和原RouterBudget账本。
只新增来源候选/调用关联/影子决定事实，提案为ignore、observe、attach、create、ask，
程序输出均为would_*且eligible_for_execution=false；不创建业务Ticket/Run/Worker，
不发送消息或发布。来源正文临时组装，原响应不进普通CAS，新表不建立第二份费用账本。

Harness固定`0e0adc6a5bb5a7e0c3a3c8323e33513e4b911d84`第32节已确认本卡0039及四处共享范围。
实际接线与批准补丁一致，0039 SQL匹配批准指纹；本树38份迁移为0001–0036、0038、0039，
未导入0037或H12。原三处财务结算AST、预算算法、ToolAuthority及旧DTO保持。
H02仅按固定169925f/fee0875选择性集成，Source使用公开流解析/能力/Token估算辅助，
使用独立请求而不伪造TaskBinding、Run或Workspace。

默认disabled。六个管理接口沿用cookie、写入Origin/CSRF、事务内会话复核及no-store。
HTTP evaluate只接受已有FastPath批次ID，202仅表示fixture排队、semantic_evaluation=NOT_RUN；
live只能由受信runtime显式排队并注入已有Root Router许可，不从HTTP或环境产生付费授权。

## 验证与版本

| 检查 | 范围与本轮结果 |
| --- | --- |
| 首次整卡注册 | 1777项：1776 PASS、1 FAIL、0 ERROR/SKIP；345项SA09新增均PASS；原结果保持FAIL |
| 原失败项修复 | 仅SA06 future负例固定验签时钟，保持301秒边界和全部原断言；新提交定向1 PASS，未重复原通过项 |
| lint | Root首次整合make lint，exit0；命令证据见下文static记录；修复后未重跑通过目标 |
| typecheck | 首次检查245文件，仅domain两处类型推断错误；等价展开谓词后只该失败文件PASS，无字段/schema变化，未重复已通过目标 |
| 当前权限和无副作用 | 真实临时PG覆盖canonical、TTL、身份/策略/预算/成员修订、精确follow_up、零业务变化；正式总结果以上述冻结首轮为准 |
| 模型与原预算 | 受控HTTP流、durable预占、known费用与安全结果原子提交、unknown/known-invalid不重发、签署上限及外层事务拒绝 |
| 恢复/迁移/兼容 | 真进程SIGKILL后原Engine.recover、ready严格JSON解码；38份迁移/FK/不可变/DDL回滚与原Routing隔离 |
| 最终完整性/清理 | PASS：9份manifest/143引用、38份安装checksum、固定6个H02 blob、旧37 SQL/三处财务AST；临时PG/会话/故障容器均0；六个主库新表0行；独立验收另列 |

开发期失败与修复均保留：聚合正文上界检查提前至Pydantic构造之前，错误不携带来源片段；
精确controller还需当前follow_up Admission；拒绝未durable的外层事务模型发送；runtime
上下文/输出上限不得宽于签署配置。预占发生但尚未POST的指标改称recorded_model_attempts，
并区分known_billed_calls和unconfirmed_model_attempts，不把预占行当作准确物理发送数。
原费用、预占、状态统计及共享Router指标不改，旧Router总量不冒充旧Worker数量。

先前各组件PASS、fixture布置FAIL、局部静态修复及定向复测不与整卡首轮拼接计数。
并行开发时全局指纹变化的组件轮仅作组件支撑，不能证明冻结整卡通过。首轮唯一失败为
`tests/test_super_agent_sa06.py::test_authentication_failures_never_persist_body_or_identity[future]`：
取整时间加301秒，经过PG查询再验签时可能落进允许的300秒窗口，导致原异常断言失败。
仅该future分支将Feishu verifier的datetime固定到生成时间，仍严格测试301秒并保留原断言。
AST核对确认其余测试逻辑、生产源码、迁移、评分器、schema和fixture不变，仅更新该测试清单hash。
修复提交只定向1 PASS，源码前后稳定；原1776通过项和静态通过目标均未重跑，不能称修复后全量通过。

## 数据与复现边界

固定快照绑定来源、身份、规则、预算签署版本、精确目标epoch和输入版本；发送前和提交时
重新核验。known成本与sanitized结果在原Engine同一外层事务提交，unknown保留原预占，
canonical输入的调用身份不能通过新配置、成员revision或新批次绕过。缺仓库、上下文、
预算或当前权限时保留候选并给固定原因，不补拉、不跟随任意URL。

200例合成草案和proposed_holdout仍需操作者逐例复核，不是独立语义holdout。冻结评分器
保留失败分母、分层精确率/召回率、零预测时undefined及不确定关联；受控协议/评分器测试
不证明真实语义质量或live能力。详细契约见[SA09说明](SA09_ROUTER_EVAL.md)。

原始证据根：`evidence/super-agent-v1/SAV1-09/`。首次整卡结果、命令与JUnit：
`evidence/SAV1-09/20261112T125824Z-1f26ecbe/`。command.log、result.json与junit.xml均由原进程生成并保留；
supervisor命令记录为本卡主证据根下
`20261112T125823Z-registered-initial-cfec76/commands.json`，不改写原评分器结果。
共享和语义只读核验：
`evidence/super-agent-v1/SAV1-09/pre-registry-design-audit-20261112T125935Z/review.json`。
最终正式摘要：[accept/SAV1-09/accept.json](accept/SAV1-09/accept.json)。
主证据根下`registered-failure-diagnosis.json`保留原失败hash与根因，
`registered-failure-repair-scope.json`记录AST/文件边界，`registered-failure-repair.json`绑定新提交和唯一复验节点。
定向命令/JUnit为`20261112T134253Z-registered-future-failed-only-fixed-df5bcd/`；
`delivery-audit.json`记录只读完整性/清理PASS（不是独立验收）。

主证据根为`evidence/super-agent-v1/SAV1-09/start-20261112T110706Z-588a9f55/`。
整合静态记录分别为该目录下的：

- `20261112T124432Z-lint-integrated-initial-a22962/commands.json`，make lint exit0。
- `20261112T124432Z-typecheck-integrated-initial-cf12c5/commands.json`，make typecheck exit2，
  日志确认245文件中仅source_eval_domain.py两处推断错误；原失败记录保留。
- `20261112T124624Z-typecheck-failed-file-only-fixed-dcbcc9/commands.json`，仅该失败文件mypy
  exit0。未重跑已通过目标，不称为修复后重新检查245文件。

静态初轮源码指纹为c7bda8…f8091，失败文件定向修复轮为9ed091…e22c；之后注册/产物清单
冻结为上列180c9b…19417。静态范围、源码版本与首次整卡结果分开记录。


仅原失败节点的复现命令如下；不重复已通过项：

```bash
cd /home/imini/magr-super-agent/v3
TMPDIR="$PWD/.local/tmp" PYTHONPATH=src:tests MGAR_V3_ALLOW_PAID=0 MGAR_V3_ALLOW_EXTERNAL_WRITE=0 \
  .venv/bin/python -m pytest -q \
  'tests/test_super_agent_sa06.py::test_authentication_failures_never_persist_body_or_identity[future]'
```

首次注册的历史复现使用本副本已配置的环回PG和R5沙箱，静默导出DSN供旧5项opt-in检查。
下面保留初轮入口，不作为修复后重跑建议：

```bash
cd /home/imini/magr-super-agent/v3
PYTHONPATH=src:tests .venv/bin/python - <<'PY'
import os, subprocess
from pathlib import Path
from mgar_v3.settings import Settings
env = dict(os.environ, MGAR_V3_DATABASE_URL=Settings().database_url.get_secret_value(),
           MGAR_V3_ALLOW_PAID="0", MGAR_V3_ALLOW_EXTERNAL_WRITE="0",
           UV_OFFLINE="1", TMPDIR=str(Path(".local/tmp").resolve()))
subprocess.run(["make", "-C", "..", "verify", "TASK=SAV1-09"], env=env, check=True)
PY
```
