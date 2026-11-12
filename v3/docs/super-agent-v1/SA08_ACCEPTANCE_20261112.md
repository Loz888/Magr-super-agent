> 当前SA08已由用户工程放行：原3项独立PASS，新增权限/撤权/别名/无副作用6 PASS；未重跑历史全量。见[关闭记录](accept/SAV1-08/independent-closure-20261112.json)。下方为历史候选交付记录。

# SA08 工程交付记录

状态：IN_PROGRESS，工程交付完成、等待独立验收。SA09未启动；真实平台、付费、
外发、发布、完整系统安全与长期门均NOT_RUN。本轮真实付费模型调用、费用及外部业务写为0。

初轮源码为`c7214a7cf9615a8f8f8381094348feb1c34a874a`，source_sha256为
`37a6188e07f510edbe4365013f312a7fe6c7f378c5b5c276f07775afcd20898b`。
L01修复提交：9950ebd4a59127199ee00e7e407d5bc50f61619d；修复后指纹：69ddeb1cce88bba4841907ae467743d563c0691d2c812a24f011e9fa6d447f27。

## 范围与验证矩阵

Harness固定`1e397ab73feeaa6e9a5d747963bb9889eec5a51f`第31节已实际确认所有权，SQL与
接线提案hash匹配。0038和app.sa_fast_path_*、FastPath模块及Engine组合/API注册/新canonical
观察hook已落地；迁移目录37份（0001–0036及0038），不导入或伪造Harness0037。
业务事实仍由原Engine同一持锁PG连接/事务写入，原预算/结算算法与ToolAuthority保持。

| 范围 | 主要验证 | 本轮状态 |
| --- | --- | --- |
| 确定性过滤 | 安装/群/用户、时效、机器人/自身结果、历史/gap、冷却及有界话题合并 | 初轮197项中的相关用例PASS |
| 权限与持久事实 | 当前Receive/身份/策略/head/TTL，origin、别名幂等、撤权/删除、容量、事务回滚与恢复 | 同上，真实临时PG |
| 控制与影子 | 精确Ticket控制角色、高优先级would_*，无真实Worker/Run/消息/发布 | 同上；另有下述L01本地失败 |
| 六个管理接口与0038 | cookie、Origin/CSRF、当前会话、no-store、迁移旧行/checksum/回滚 | 初轮真实PG/API检查PASS |
| 首次完整注册 | 新增197项及原1161项，共1358项 | 1358 PASS，0 failure/error/skip，exit0 |
| 静态 | 初轮make lint/typecheck，238个源码文件；新增3项回归所在两个文件首次ruff | PASS；分别保留原检查范围与源码版本 |
| L01修复后定向 | 只复测原3个失败，迁入正式注册后总1361项 | 3 PASS，0 failure/error/skip，exit0；修复后全量NOT_RUN |

## SA08-L01 本地发现与修复记录

飞书post开头的at节点被规则解码为空格，可信mention后的`/mgar pause|cancel <UUID>`因此
降为explicit_request，丢失控制意图。初轮冻结源码上，纯解析/原Adapter两项及真实PG一项
合计3FAIL：后者未取得预期priority100/would_pause，而成为普通context观察。
现仅保留固定at占位符以恢复严格命令识别；平台可信mention、当前身份、controller及
精确Ticket权限检查继续生效。原函数AST/断言保留，定向3 PASS，原3 FAIL及此前失败均保留。

本次按用户约定只复测原失败项。初轮完整注册与修复方定向结果分开记录，不能将二者
拼接为“修复后全量1361PASS”；初轮已通过的lint/typecheck不重复运行。

原始证据根为`evidence/super-agent-v1/SAV1-08/start-20261112T081641Z-5eb84f0a/`。
其下`20261112T101143Z-rich-post-pure-original-2e0482/`保存2FAIL，
`20261112T101322Z-rich-post-pg-original-6db5e7/`保存1FAIL；各含commands.json、日志与JUnit。
初轮注册原评分器结果位置为`evidence/SAV1-08/20261112T100023Z-ac25e128/`，
静态记录在`20261112T095423Z-{lint,typecheck}-integrated-initial-*`；新增文件ruff记录在
`20261112T101918Z-rich-post-new-files-lint-initial-e9f785/`。修复后定向证据：evidence/super-agent-v1/SAV1-08/start-20261112T081641Z-5eb84f0a/20261112T103557Z-rich-post-failed-only-fixed-33e8cd/commands.json。

## 本地复现与能力边界

使用本副本已配置的环回PG和R5沙箱。下列命令静默导出同一DSN；旧仓库分析5项检查要求
进程MGAR_V3_DATABASE_URL，仅由Settings读取.env不满足其opt-in开关。默认只运行原3个失败：

```bash
cd /home/imini/magr-super-agent/v3
PYTHONPATH=src:tests .venv/bin/python - <<'PY'
import os, subprocess, sys
from mgar_v3.settings import Settings
env = dict(os.environ, MGAR_V3_DATABASE_URL=Settings().database_url.get_secret_value(),
           MGAR_V3_ALLOW_PAID="0", MGAR_V3_ALLOW_EXTERNAL_WRITE="0")
cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short",
       "tests/test_super_agent_sa08_post_rules.py", "tests/test_super_agent_sa08_post_controls.py"]
# 复现初轮完整注册时，在c7214a7使用：
# cmd = ["make", "-C", "..", "verify", "TASK=SAV1-08"]
subprocess.run(cmd, env=env, check=True)
PY
```

固定源码只产生无正文决策、would_*候选与当前鉴权预览；显式@及auto策略模式也不执行真实
动作，Router语义评估NOT_RUN。合成确定性benchmark用于规则回归，并非经操作者复核的
独立语义holdout；SA00的200条dev/holdout协议与SA09评估仍待相应阶段落实。
最终37份安装checksum、临时PG/会话/故障容器盘点：evidence/super-agent-v1/SAV1-08/start-20261112T081641Z-5eb84f0a/delivery-audit.json。
实施方工程结果不构成独立验收；范围详情见[SA08说明](SA08_FAST_PATH.md)。

初轮评分器的HEAD记录仍为启动时7eabf7b；其冻结源码与随后提交c7214a7的Git快照指纹
完全一致，绑定证明为`evidence/super-agent-v1/SAV1-08/start-20261112T081641Z-5eb84f0a/frozen-commit-equivalence.json`，原result未改写。
终审99项artifact及修复最小范围见`evidence/super-agent-v1/SAV1-08/implementation-audit-20261112T092418Z/final-repair-audit.json`。
