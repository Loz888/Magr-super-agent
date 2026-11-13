# SA10-I01 正式集成修复与工程交付

SA10 本地工程复验完成，保持 IN_PROGRESS，等待独立复验；SA11 未启动。
源码候选 `dcea520250e7d0d361cd451e20211b1fc7a86432`，指纹
`dd47a56701afcee6a8257e1ec152d48556cc7feea6d253a6e324c4c0cd326e87`。
完整机器记录见 [accept.json](accept/SAV1-10/accept.json)。

## 正式接入

原独立候选 e9ee378 的 SA10-I01 两项 FAIL 属实：只有 SQL 草案和私有组件，正式
0041、Engine 组合及 API 注册尚未落地。原报告、失败日志和 JUnit 未修改，公开快照见
[独立结果](accept/SAV1-10/independent-readiness-20261113.json)。

用户转达的 Harness 固定 `0a335a14d8ce37703b5cfe0eaa7960d4ac571fc7` 第34节已只读
核对，且该提交仅含协调文档。请求 SHA `61e7f271…127c43`、SQL SHA
`d6aead81…fec1ee3` 和补丁 SHA `352277eb…19c278` 的完整值逐项匹配。

集成提交 `191ed23753b590b61f76b4dfe5f9c3a4e54cd085`：

- 正式 `migrations/0041_source_correlations.sql` 与获确认草案逐字节相同；通过原
  `make migrate` 安装，仅新增0041。当前39份已安装checksum全部匹配。
- 原 Engine 仅增加 import 和 `SourceCorrelations(self)` 组合；原 api.py 仅增加
  import 和六个管理接口注册。两文件实际字节精确等于获确认补丁的应用结果。
- API 测试使用原 create_app，移除阶段性的测试专用注册组合；实际测试函数及
  业务断言 AST 不变。五份旧目录测试仅追加当前0041期望，原升级基线不改。
- 当前10份产物清单169个引用匹配。原38份 SQL、三项结算方法、SA09-I01生产
  修复模块、H02固定六文件不变；没有0037/0040、H12或Harness活动树导入。

## 正式检查与失败项修复

用户要求的范围精确为原两项守卫复验，加其余199项首次正式检查；未重新执行旧
私有组件流水线。使用原评分器执行 `make verify TASK=SAV1-10`：

| 候选 | 实际范围 | 结果 | 源码指纹 |
| --- | --- | --- | --- |
| 191ed23 | 原注册201项：2复验、199首次正式 | 200 PASS / 1 FAIL，0 error/skip | 073d3aea…bd3f9f8 |
| dcea520 | 只剩余失败的接线守卫 | 1 PASS，0 failure/error/skip | dd47a567…326e87 |

正式 SQL 守卫在首轮通过，其余199项首次正式检查全部通过。接线守卫已经核对真实
Engine 及原持锁连接，随后在路由枚举遇到 FastAPI `_IncludedRouter` 没有 path
属性而中断。只将这个失败守卫的过滤改为 `getattr(route, "path", "")`；六个精确
路由/方法、Engine和原连接断言的AST全部保持，生产源码未改。

固定 dcea520 后仅该原失败节点复验，1 PASS、退出0，源码指纹前后一致。此前200项
不重复；原201轮的 FAIL 与原评分器结果保留，不宣称最终源码单轮201PASS。
逐例账本核对了两个原失败节点及199首次节点，合计201个正式用例都有对应通过证据。

原始路径（相对于v3）：

- 首轮原评分器：`evidence/SAV1-10/20261113T034518Z-84ae9b20/`。
  pytest退出1，make退出2；result.json、command.log、junit.xml保留原样。
- 原失败单项：`evidence/super-agent-v1/SAV1-10/integrate-I01-20261113T034036Z-0de9b080/20261113T034949Z-sole-failed-route-guard-only-014bf0/`。
- 同一集成证据根下：`formal-case-outcome-accounting.json`、
  `route-guard-compatibility-repair/repair-scope.json` 和
  `api-formal-composition-test-integrity.json` 记录范围与断言保护。

当前候选的单项复验命令（仅在需要复验原失败时执行）：

```bash
cd /home/imini/magr-super-agent/v3
PYTHONPATH=src:tests TMPDIR="$PWD/.local/tmp" .venv/bin/python -m pytest -q \
  tests/test_super_agent_sa10_migration.py::test_real_engine_and_unpatched_application_compose_original_connection_and_six_routes
```

## 静态与完整性

本次仅对新接线及变更的夹具/目录文件做定向 lint，8文件PASS，没有重复历史整库
lint/typecheck。第一次定向 mypy 从仓库根执行，未加载应用配置中的 Pydantic 插件，
出现 API 的两个 Settings 参数误报；显式加载原配置后仅该失败 API 文件PASS。
没有为此修改生产源码，原失败记录保留。最后一处守卫枚举兼容性修复只复验失败
pytest，没有重复已通过的静态目标。

只读最终审计见集成证据根的 `delivery-integrity-audit/final.json`：39份安装checksum、
169个引用匹配；主库新五表均0行，临时PG、会话、故障容器0。原历史FAIL、SA09独立
闭环和用户文件保留。更新的活动准备摘要与SA09验收元数据均有旧字节history副本，
分别精确匹配原e9ee378中的文件；未覆盖独立原始报告。

## 依赖与放行边界

SA09-I01实际已有独立原6项PASS闭环：ee1fee3/76fc815b，六场景均零POST。当前元数据
已同步[既有独立关闭记录](accept/SAV1-09/i01-independent-closure-20261113.json)，
不是新增独立检查，也不把旧93e9a9d的FAIL抹去。

SA10本地 engineering_gate 为PASS，依据上述分版本正式证据；当前修复候选仍待独立
复验。完整系统安全、语义质量、真实平台、付费、真实编码、外发、发布及长期门
保持NOT_RUN。SA11不推进；本地开发和Harness所有权确认均不授予这些真实能力。
