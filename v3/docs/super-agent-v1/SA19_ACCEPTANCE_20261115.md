# SA19 本地受控工程交付（待独立验收）

源码候选 **7c2d7687a092a714fa58adf3616f314c40d38ac9**，官方源码指纹 **dc6af02f523f3fe997b5446b8eada45f518d1cdec043bc3c6528c2d5fc159371**。

**78个正式独特用例均有分版本PASS证据，未在最后源码重跑全量。** 另有准备范围54PASS，未重跑。SA19 保持 IN_PROGRESS 等待独立验收；SA20 未启动。全部真实平台、付费、编码、外发、发布及长期运行门仍 NOT_RUN；固定 H12 真实 Agent/MVP FAIL 保留，另一CLI负责。

[逐例机器摘要](accept/SAV1-19/accept.json)包含每例源码提交、指纹和JUnit路径；[管理员流程](SA19_CONSOLE.md)说明安装范围、角色、预算、连接、撤权、停止和恢复限制。

新增账户首页入口与 `/source-console`，组合原 Engine、Source、四层策略、可信身份、共享预算、原 Task/Assignment/Job/Action。可以通过网页保存安装/群/用户/凭据与规则，预览、绑定网关/影子观察、设置原组织和项目共享额度、查看决定及费用、撤销来源或全部来源，以及停止精确原任务。连接在线和权限不足分开显示，NOT_RUN 和模型提案分开显示。自动修复在界面和服务端均关闭，关闭浏览器不取消任务。

0052及三文件精确接线依据 Harness **8d19308a585e80885fc4f0bfeec4853395236ab8 第45节**，确认已核对且已在SA副本应用；未导入或改动Harness活动树。正式 SQL SHA256 `765932acca6566a33596e2c543b446c0675ccce689db9e82223b64cf9cd14fb4`，补丁 SHA256 `b2ba32c532005d4d798f9230c5e1b6d0676ff300a720e62f4a1f2fef7eeb22f8`。原 Engine/API/agent.html 三文件与确认后哈希完全一致。注册12个Owner API与3个页面/资源接口；仅新增加密凭据当前值、无正文审计两表，无第二业务状态机。

首次及定向修复证据如下，表内数量不混入静态检查或准备54项：

| 范围 | 首次 | 仅失败项复验或首次增量 | 独特通过 |
| --- | --- | --- | --- |
| 原连接/迁移/保管/CAS/撤权/停止后端 | 23 PASS / 2 FAIL，4856d90 | 73cfb6f 原2 PASS | 25 |
| HTTP权限、Origin/CSRF、边界、脱敏、健康与元数据 | 4 PASS / 27 FAIL，907deb2 | 140dab9 原27中26 PASS / 1 FAIL；fb61677 仅余1 PASS | 31 |
| 浏览器初始设置、健康、关闭与停止 | 0 PASS / 3 FAIL，907deb2 | 140dab9 仅原3 PASS | 3 |
| 决定溯源、当前proof、静态路径/会话及HTTP撤权 | fb61677 首次8 PASS | 8fc7494 新增路径归一化/HEAD首次5 PASS | 13 |
| 窄屏Shadow解释和未评估展示 | f2e220e 首次1 PASS | 未重跑 | 1 |
| 契约、18份Source目录、注册和交付 | f2e220e 首次5 PASS | 未重跑 | 5 |

31项HTTP及后续13项边界使用真正的原Owner、原Engine和隔离PG；浏览器是实际Chromium＋uvicorn＋cookie/CSRF。没有真实平台或付费模型请求，原任务由正常 Engine.ingest 建立；停止检查原 status/epoch，关浏览器后再次读取证明未取消。凭据输入清空、DOM无原值、无浏览器持久存储、Shadow无Worker/Action/模型调用均有断言。原始失败及修正后的探针各留其提交/归档；没有通过修改服务端允许列表来掩盖测试环境错误。

保留的错误和修正：

- 后端首轮含FastAPI懒路由清单漏12接口，以及测试错误地要求原Engine向外项目 ingest。私有路由直接注册；外项目探针使用合法的顺序双Engine建原任务，再从本项目拒绝读/停。
- 初HTTP探针使用 `testserver`，被原TrustedHost正确拒绝；修正为原已允许的127.0.0.1:18081，并进入原应用lifespan。来源身份绑定会推进auth_revision，旧proof不可用的预期据既有契约纠正；新增独立用例验证新可信事件才可产生当前proof。原失败没有删除。
- 浏览器实际发现首次项目预算缺少原组织预算，现由明确的网页首次组织设置解决。浏览器测试socket改为可复用，避免上一轮关闭后的TIME_WAIT挡住下一例。
- 原 `/static` 挂载位于注册接口之前，会绕过后定义的资源Owner检查。仅在私有控制台模块调整其两条资源优先级，并拒绝页面别名、点路径/编码/双斜杠别名以及HEAD回退；原共享API补丁字节保持不变。
- 最后HTTP轮换探针曾误用 `credential_valid_until`，原轮换契约字段是 `valid_until`。仅此失败函数重新执行1PASS，原30项HTTP通过不再运行。
- 多轮lint长字符串、函数换行和导入分组错误均完整保留；最后7c2d768只复验失败lint目标并收集清单，未执行已通过用例。

静态检查按源码分版记录：准备初轮完整typecheck312文件PASS；后端最终83fe1ab七文件PASS；新投影907deb2两文件PASS。初完整lint一处失败仅失败目标修复PASS；后端失败lint目标73cfb6f通过，页面阶段失败lint目标到7c2d768通过（3个实现文件＋5个测试/辅助文件）。按用户规则未重复已通过typecheck目标；后续私有静态回退中间件有lint和行为证据，**不称最后源码重新全量lint/typecheck通过**。

全部11轮先提交、生成不可变Git源码归档、重算官方指纹确认归档与工作树一致，检查前后指纹一致。最后 `pytest --collect-only` 收到78个正式node，逐一与历次PASS JUnit匹配，旧失败观测33条保留。早期准备/后端三个轮次通用runner遗留 `PREPARATION_ONLY` 标签，不能据此误解正式范围；对应scope、命令、正式49迁移和JUnit记录是真实依据，原始标签不改写。

交付前只读核对：仓库49份迁移，18份Source目录共584条产物引用均匹配；原17份目录保留在 `accept/SAV1-19/history/`，历史失败和用户文件未改。SA主库55435仍44份迁移、校验和匹配、无0052两表，未安装本卡SQL。11轮所有隔离PG容器/测试库已清理；本卡浏览器进程与只读核对会话无残留。原H06容器与其他CLI资源保留。

原始证据根目录为 `evidence/super-agent-v1/SAV1-19/start-20261115T084702Z-1880540c/`：`provenance-full.json`、`readonly-closeout.json`、各轮命令/日志/JUnit/源码归档。初三项浏览器截图缺Linux中文字形，DOM检查有效；新增窄屏影子场景只读使用本机已有Windows中文字体，截图为 `delivery-and-mobile-first/pytest-temp/test_browser_shadow_reason_and0/mobile-shadow-observation.png`。未分发该字体。

复现需使用隔离PG与锁定环境，真实浏览器需本机Chromium及运行库。完整注册入口 `make verify TASK=SAV1-19`，子范围为 `SAV1-19-PREPARE`、`SAV1-19-HTTP`、`SAV1-19-BROWSER`、`SAV1-19-BOUNDARIES`、`SAV1-19-DELIVERY`。修复仅运行失败node/target；已通过范围按机器摘要中的源码归档复查，不能把新源分版本成绩改写为同版本全量PASS。

列表为有界元数据观察，来源设置不自动组合或激活实际Runtime，真实部署与长期运行还需后续任务和单独许可。此报告只交付SA19本地受控工程，独立验收与全部真实能力门另行判定。
