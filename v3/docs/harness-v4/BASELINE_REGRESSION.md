# Harness V4 基线回归日志（HV4-H00）

状态：**FROZEN**。机器可读基线：`experiments/harness-v4/baseline.json`。
本日志只记录在本机 WSL2 环境中实际执行的命令与结果；未执行的项明确标记为 NOT_RUN。

## 1. 基线与环境

- 仓库：`https://github.com/Loz888/MAGR-v3.git`，分支 `main`。
- HEAD：`973994c0e6c087c2968b3d91a3a6b9220e2c0892`（含 harness-v4 规划文档与近期 UI 回归）。
- 应用基线：`1d07285aa27ece9bae4ccbedc3a77ce2fbcbaf3a`。
- 工作区：`/home/imini/magr-harness`（WSL2 Linux 文件系统，独立于旧工作区）。
- Python 3.12.16，uv 0.13.7；`uv.lock` sha256 `a0597500…174d9`。
- PostgreSQL `mgar-v3-postgres-1`（pgvector 0.8.1 / PG16，healthy）；
  Redis `mgar-v3-redis-1`（7.4.7，healthy）。
- R03 沙箱镜像：`sha256:efad10932c12f9d6f2408fe960e9a292c4bdbac0f8365387838acbe4f0fdd2b6`
  （lock sha256 `2b6ffb5cead4d18e1219816c4d829c7f2cdf3dc5165cf1683fb4c25f360f3e86`）。
- R5 沙箱镜像：`sha256:daa7afaec0e3e97db1bfcf522d63ca08f75e28289232b263f1c5802d94eb7b9c`
  （lock sha256 `6465789b3feb2352f396d488001d5d67148935bfeb144ab0c67c6b45bfb07255`）。
- 本机 `.env` 从旧工作区复制并按当前容器口令重新对齐；未输出任何密钥。

## 2. 静态检查

初次测量（基线提交未修改）：

```text
uv run --frozen ruff check src tests scripts   # 23 errors
uv run --frozen mypy                           # 16 errors
```

分布：ruff 集中在 `repository_analysis/tools.py`（6）、`test_v32_repository_analysis.py`（6）、
`report.py`（3）、`quality_eval/record.py`（2）、`tests/test_quality_eval_records.py`（2），
以及 `manifest.py`、`stats.py`、`validate.py`、`scripts/quality_eval_import_v6_rehearsal.py`
各 1。mypy 集中在 `fixture.py`、`tools.py`、`service.py` 与 `stats.py`。

修复后复测：

```text
uv run --frozen ruff check src tests scripts   # exit 0, 0 errors
uv run --frozen mypy                           # exit 0, 0 errors
```

原始输出保存在 `evidence/harness-v4/HV4-H00/h00-20261110T120708Z/` 与
`h00-20261110T121023Z/`。修复只涉及导入排序、未使用导入、行宽、显式类型标注与
等价的重命名，不改变业务语义；受影响模块的回归见下一节。

## 3. 核心回归

受影响模块的先行检查：

```text
uv run --frozen pytest -q tests/test_v32_repository_analysis.py \
  tests/test_quality_eval_records.py tests/test_quality_eval_stats.py \
  tests/test_quality_eval_validation.py tests/test_agent_page.py
# 57 passed
```

冻结核心回归集（协调引擎、效果/恢复、仓库分析、补丁工作区、R03 沙箱、安全恢复、
V35-D01、V35-E03、API/网页、评测记录/统计/校验、verification），共 20 个文件：

```text
uv run --frozen pytest -q <20 core files> --junitxml=.../core-regression/junit.xml
# 225 passed, 0 failed, 0 errors, 0 skipped, 265.692s
```

最终运行导出了 `v3/.env` 中的真实 PostgreSQL DSN，DB 集成测试未再跳过。证据：
`evidence/harness-v4/HV4-H00/core-regression/junit.xml` 与 `evidence/harness-v4/HV4-H00/core-regression/pytest.log`。这是 H00 可复现的核心回归证据，
不把它扩大解释为全部 922 个测试通过。

### 3.1 失败尝试原样保留

新工作区最初缺少沙箱 lock，核心集曾出现两次失败：

| 尝试 | 结果 | 原因 | 是否计数 |
| --- | --- | --- | --- |
| 0 | 23 failed, 196 passed, 4 skipped, 2 errors | 缺少 `.local/r03/sandbox-lock.json` | 否 |
| 1 | 1 failed, 220 passed, 4 skipped | 缺少 `.local/r5/sandbox-lock.json`（sigkill 恢复测试） | 否 |
| 2 | 225 passed | 执行 `make sandbox-build` 与 `make sandbox-build-r5` 后 | 是 |

尝试 0 的日志保存在
`evidence/harness-v4/HV4-H00/core-regression-attempt1/pytest-attempt0.log`；
尝试 1 的 junit/log 保存在同目录。它们没有被删除或改写为通过。

全量套件曾以 `pytest -q` 收集 922 个测试；运行超过十分钟没有进展（pytest 处于
等待、PostgreSQL 会话空闲、无沙箱容器活动），因此被停止，状态记为
`NOT_COMPLETED`，不计为通过。该限制已写入 `baseline.json` 的 `regression.full_suite`。

## 4. Live 门与历史失败

H00 没有执行任何付费模型调用、真实 GitHub 写入或外网发布，因此：

- `live_model_gate`：`NOT_RUN`（H00 自身）；
- `live_publication_gate`：`NOT_RUN`；
- 历史记录原样保留：V35-E02 四臂全部 `PATCH_TRUNCATED`，共 12 次真实调用、
  637310 micros，结论 `INCONCLUSIVE`；V35-E02 的 `live_model_gate` 与
  `live_agent_gate` 仍为 `FAIL`；
- V35-D01 的聚合记录保持工程/安全 `PASS`、Live 模型 `FAIL`、GitHub 写 `NOT_RUN`。

旧失败没有被 fixture 或新文档覆盖，也没有被转写为 PASS。

## 5. 结论与边界

H00 的工程结论仅覆盖：全库 lint/typecheck 为 0、核心回归集通过、24 个任务与
判定器哈希冻结、协议与能力审计可机器校验。它不证明真实模型能完成编码任务，
不证明外部发布可用，也不证明全部 922 个测试通过。
