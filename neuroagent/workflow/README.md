# Workflow 层

`neuroagent.workflow` 实现审批后运行的状态机与独立 Worker。运行只能从仍有效的已批准 `PlanRevision` 创建；计划审批前状态不属于 Workflow。

当前能力包括：

- `queued → running → qc_review → succeeded` 以及取消、超时和失败状态；
- SQLite 原子任务领取、Worker 租约和心跳；
- 有限重试、崩溃后租约恢复和幂等结果收口；
- 通过持久化事件提供可续传的 SSE 运行记录；
- 通用/指标 Mock 成功后进入人工 QC，不把部分产物误报为成功；`statistics_mock` 只验证运行协议并直接进入 `succeeded`，其 `mock.result` 不得声称为科研统计产物。

本层不选择 Skill、不解释科研参数、不执行模型推理，也不接受任意命令。组合根当前把 `MockJobExecutor` 直接注入 Worker；公共运行通路尚未遍历 `SkillPlan` DAG 或通过 `ToolRegistry`/`ToolRuntime` 分派类型化 Tool。受控 MATLAB Executor 已独立实现，但未接入该通路。
