# SA05 历史补拉独立复验修复候选

实现 cf7c17662f262b52e04cf835fdcdb0c9e6fb901f，基线 0548a0f34de8e80b6d0a26002f3540d2a27cb12d。源码 SHA-256 890e475b2089ed516e2cabe01dd59928bb25f23b971c1faaf04f657ed8977b47，完整注册前后相同。状态 ENGINEERING_PASS_PENDING_INDEPENDENT_REVIEW；SA05 IN_PROGRESS，SA06 PLANNED。

## 修复

- RR01：当前 API 评论即使作者未选入，也提交可信无正文版本观测，由唯一 Engine 持久最小拒收/失效事实。新正文/body_ref不保留、不签发作者身份、不改变角色。未知作者类型拒绝。row.user 仅表示内容作者，历史保持 create + historical_backfill + installation_proof 的 authored snapshot，不伪造最后编辑者或评论编辑事件。无原生排序继续 sticky ambiguous。
- RR02：已验签实时资源与已鉴权安装 API 的相同资源，使用一致的 platform_moderation 资源证明。等价创建/历史按任意顺序收敛一个 canonical input，不删除已有正文、不将 history-first 输入升级为 live。通用 ItemMetadata.fingerprint 完全未改，仍严格包含 moderation、真实内容、actor、scope、operation；负例保留真实冲突拒绝。
- History 也核对当前策略 actor 不超过 App sender/context-bot 配置；mint/GET 前后及 Engine 逐行当前 Receive 检查保留。无历史许可不发生远端读取，历史读取许可不扩展每行正文或控制权。

私有 GitHubAdapter/GitHubReadBroker/GitHubHistory 修改，无 Engine/API/DTO/schema/迁移/预算/ToolAuthority/Harness 变化；无需新共享所有权或迁移编号。已有固定确认79798690 §24、a04dbde §17沿用，不声称新增消息送达或确认。

## 实际结果

| 检查 | 结果 |
| --- | --- |
| 本次独立3项，修复前原样重现 | 3 FAIL |
| 新正式23项，修复前 | 15 FAIL / 8 PASS |
| 修复后正式历史回归 | 23 PASS |
| 本次独立3项原样重跑 | 3 PASS（修复方重跑） |
| 前一轮独立10项原样重跑 | 10 PASS（修复方重跑） |
| make verify TASK=SAV1-05 | 566 PASS，0 failure/error/skip |
| make lint / make typecheck | PASS / PASS（222 source files） |

566 = SA05原79 + 上轮修复44 + 本轮23 + 既有420。正式结果由原 scripts/verify.py 生成：evidence/SAV1-05/20261111T054311Z-1df918b3/result.json；JUnit与完整日志同目录，wrapper记录 evidence/super-agent-v1/SAV1-05/20261111T054311Z-history-repair-registered/commands.json。静态 evidence/super-agent-v1/SAV1-05/20261111T054112Z-history-repair-static/commands.json；专项 evidence/super-agent-v1/SAV1-05/20261111T054017Z-history-repair-regression/commands.json；原独立重跑 evidence/super-agent-v1/SAV1-05/20261111T054108Z-history-repair-review/commands.json 和 evidence/super-agent-v1/SAV1-05/20261111T054111Z-history-repair-prior/commands.json。独立测试函数AST保持原样；旧测试/迁移/schema/评分器196份文件字节级不变。

原独立报告 .local/review-sa05-repair-independent-20261111T050333Z/review-result.json 与 feedback、探针、失败日志/JUnit均未改。该候选原543项注册、静态和原10项PASS，但新增3项FAIL的结论保留，不能以工程绿覆盖。上轮验收原样存档 accept/SAV1-05/history/0548a0f-accept.json，前一修复与更早FAIL/Hangup档案未改。本轮摘要 accept/SAV1-05/history-repair-20261111.json。

Linux本副本的等价复现命令（有效工作目录/环境）：

    cd /home/imini/magr-super-agent/v3
    UV_OFFLINE=1 TMPDIR="$PWD/.local/tmp" PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python -m pytest -q -s .local/review-sa05-repair-independent-20261111T050333Z/test_independent_history_boundaries.py

原样复现、正式修复前后、静态和完整注册均有独立新目录。初次ruff格式检查失败单独保留；没有弱化断言或修改评分器。UTC阅读/文件SHA、原失败文件SHA、兼容性和本副本PG只读状态见 evidence/super-agent-v1/SAV1-05/20261111T053624Z-history-repair-start/。PG16.12、34项安装迁移校验和全匹配，来源配置数0，未启用真实Runtime。

## 限制

只有engineering_gate PASS。完整policy_security_gate、live_source_channel_gate、live_coding_gate、live_publication_gate、continuous_operation_gate均NOT_RUN。真实GitHub API、模型/付费、监听/机器人、外发/发布为0；H系列没有操作。本轮受控HTTP使用MockTransport，不能给真实GitHub平台门PASS。

无严格原生顺序时真实更新也可不可读，不自动清除歧义。历史仅当前评论，不能恢复删除/全部编辑/CI投递；API响应2MiB、页100行/100页等既有限额保留。发布前实时create证明指纹纠正后，旧sealed合成回执不回写、不重授权，规范化矛盾仍明确拒绝。本持久副本没有旧真实来源配置/回执。

候选停在SA05等待新的独立复验；修复方对13项探针的重跑不代表独立放行，不进入SA06。
