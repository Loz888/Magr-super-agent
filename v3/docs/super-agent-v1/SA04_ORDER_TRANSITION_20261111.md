# SA04 排序转换修复（待独立核验）

源码提交 b15937d6dd1ecacdf720199fcd14534c05ffd986，基线 666e8fe。SA04 保持 IN_PROGRESS，未进入 SA05；本轮只处理 Super-Agent。

原因：拒收路径推进数字 watermark 时，没有保留既有无序版本带来的不可比较性。角色恢复后的数字版本因此被错误当作较新。

修复：允许和拒收路径共用排序分类，并从既有不可变 source_ingress_inputs.native_order 保留无序历史。新数字不能补造旧版本顺序。歧义与回执在原 Engine/PG 事务内提交/回滚，旧正文失效，拒收正文不保存；同版本 canonical 等价去重保持优先。全部有序的版本链仍可正常接收较新内容。

- 原独立排序差分：修复前 1 PASS/1 FAIL，原样保留；正式回归保留原函数 AST/断言。
- 新排序回归：25 PASS，覆盖 create/edit、接收/拒收、中间撤权/恢复、较旧/较新尾版本、有序正例、旧数字水位与 PG 回滚。
- 原独立新旧探针：7 PASS；旧修复与独立探针合计 28 PASS。这是修复方重跑，不是独立放行。
- 完整注册：420 PASS（SA04 116），0 failure/error/skip；含真实 PG pause、四 SIGKILL、32→33 升级及既有回归。
- lint/typecheck：PASS，215 source files；持久自有 PG 33 项 checksum 匹配，生产入站表全部为空。

源码 SHA-256：bc75f5cec156b069acb9c9434b2d11d589ebb9970501b5c556b2e036ff628d9c，注册验收前后一致。
正式证据：evidence/SAV1-04/20261111T013604Z-1901c9b0/result.json
修复摘要：docs/super-agent-v1/accept/SAV1-04/order-transition-20261111.json。
阅读、兼容、原始失败与逐例证据：evidence/super-agent-v1/SAV1-04/20261111T012548Z-order-start.

没有修改 Engine/API/DTO/schema、0001–0033 迁移或预算，没有 H 系列工作。没有启用真实平台/SDK、模型、通知或发布；付费调用/外部业务写为 0。完整系统安全、真实编码/发布、长期运行仍 NOT_RUN。未知顺序保持保守歧义，本卡不提供自动解除入口。等待独立核验后才可决定下一卡。
