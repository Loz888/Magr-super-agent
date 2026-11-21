# MAGR Super-Agent

MAGR Super-Agent 是一个事件驱动的持续研发 Agent 原型：使用者提出一个研发问题后，可以在后续消息和事件中补充约束、查看进展，并根据获准的反馈和证据审查结果。系统围绕来源、任务关联、调度和交付组织持续工作；具体接入与验证范围见[当前状态](v3/docs/CURRENT_STATUS.md)。

一条 2026-11-18 的真实试用记录展示了后台执行和反馈：使用者确认关闭飞书客户端后，Worker 完成两次编辑和一次公开检查，原话题随后收到基于任务回执的状态回复。该次正式 Verifier 为 `incomplete`，业务任务仍待放行；客户端关闭不代表主机关机后仍可运行。[案例与验证边界](v3/docs/PROJECT_OVERVIEW.md#关闭客户端后任务如何继续反馈)

## 一个问题如何持续推进

- **接收与关联：** 对来源、身份和权限进行检查，将获准消息关联到问题上下文；重复事件和不确定关联需要分别处理。
- **接续工作：** 有效订阅可将后续输入交给原任务；需要路由时提出受限候选，由 Engine 重新核对策略和授权。
- **后台执行：** 调度器安排执行尝试，Worker 在相应工作区执行并返回产物与回执；Harness 提供单次执行能力。
- **验证与反馈：** 使用者可以查看状态、审查差异与证据；验证、消息送达、业务完成和批准发布是不同的判断。

以上是组织任务过程的设计说明，实际能力按[当前状态](v3/docs/CURRENT_STATUS.md)所列版本和验证范围理解。[项目说明](v3/docs/PROJECT_OVERVIEW.md)

## 当前边界

公开基线中的 2026-11-18 记录显示 SA22 进行中：飞书试用已有真实编辑、公开检查和原话题回复；正式 Verifier 仍 incomplete。正式路由评分、完整跨渠道联动和长期运行门尚未完成，发布门为 NOT_RUN。后续本地试用有局部进展，未纳入本仓库公开基线，不能据此将完整联调记为通过。[状态、来源与未决事项](v3/docs/CURRENT_STATUS.md)

## 与 Harness 的关系

[MAGR-Harness](https://github.com/Loz888/MAGR-Harness) 关注单次获准任务的执行、验证与恢复；Super-Agent 关注同一问题跨事件接续。历史交接固定了具体契约及接收范围；尚未找到两个公开基线完成联合部署验收的记录。[版本与验证范围](v3/docs/CURRENT_STATUS.md#与-harness-的关系)

## 开始使用

准备 Git、Make、uv、Python 3.12、Docker Engine 和 Compose V2。Windows 用户可参阅 [WSL2 说明](v3/README.md#wsl2-首次启动)。

```bash
git clone https://github.com/Loz888/Magr-super-agent.git
cd Magr-super-agent/v3
uv python install 3.12
make setup
make deps-up
make migrate
make doctor
make dev
```

打开 `http://127.0.0.1:18080`，首次使用创建管理员账户。应用入口、模型设置与运行依赖见[应用说明](v3/README.md)；真实平台接入、模型调用和外部写入需各自授权与配置，不能由启动页面推断验收通过。

[项目说明](v3/docs/PROJECT_OVERVIEW.md) · [实验解读](v3/docs/EVALUATION_SUMMARY.md) · [文档导航](v3/docs/README.md) · [历史专项任务书](v3/docs/super-agent-v1/README.md)
