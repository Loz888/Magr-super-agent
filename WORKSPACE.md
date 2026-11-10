# magr-super-agent 本机工作副本

此目录从 `/home/imini/MAGR-v3` 的干净 V3.5 基线独立复制。
后续升级在当前目录进行；当前实施状态见
`v3/docs/super-agent-v1/IMPLEMENTATION_STATUS.md`。

## 本机命令

从项目根目录执行下面的命令。根 Makefile 固定独立 Compose 项目和本项目临时目录。
原 `v3/Makefile` 是上游通用文件，直接在其目录执行默认 deps-up 会指向旧项目名及端口；
这份工作副本使用根入口。

```bash
cd /home/imini/magr-super-agent
make setup
make deps-up
make migrate
make lint
make typecheck
make verify TASK=AGENT-UI
make verify TASK=V35-R05
```

PostgreSQL：`127.0.0.1:55435`；Redis：`127.0.0.1:56381`。
Compose 项目名 `magr-super-agent`，专用 volume `magr-super-agent_postgres`。
`.env` 为独立随机本地凭据，权限 0600，付费与外部写均为关闭；不要复制原项目密钥。
依赖服务已启动，应用服务尚未启动。

仅停止本副本依赖：`make deps-stop`。下一张任务卡开始前，完整阅读任务书并记录证据。

## 当前分工

Codex 负责 Super-Agent，Trae 负责 Harness。详见 `v3/docs/super-agent-v1/COORDINATION.md`。每卡实施前完整阅读任务书。
