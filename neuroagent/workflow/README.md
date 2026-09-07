# Workflow 层

`neuroagent.workflow` 实现审批后运行的状态机与独立 Worker。运行只能从仍有效的已批准 `PlanRevision` 创建；计划审批前状态不属于 Workflow。

当前能力包括：

- `queued → running → qc_review → succeeded` 以及取消、超时和失败状态；
- SQLite 原子任务领取、Worker 租约和心跳；
- 有限重试、崩溃后租约恢复和幂等结果收口；
- 通过持久化事件提供可续传的 SSE 运行记录；
- 通用/指标 Mock 成功后进入人工 QC，不把部分产物误报为成功；`statistics_mock` 只验证运行协议并直接进入 `succeeded`，其 `mock.result` 不得声称为科研统计产物。

本层不选择 Skill、不解释科研参数、不执行模型推理，也不接受任意命令。组合根把 `MockJobExecutor` 和受控 MATLAB Executor 注入 Worker；公共运行通路按队列中的显式 executor type 分派。Mock 工作流会通过 `WorkflowFactory`/`ToolRuntime` 遍历已批准的 `SkillPlan` DAG，MATLAB 统计路径会从已批准统计设计编译类型化 `MatlabJobSpec`；预处理路径从冻结 SkillPlan/manifest 编译 DPARSFA JobSpec，使用独立 attempt 工作目录并登记实际产物元数据。

MATLAB 路径由 registry 内的 `MatlabJobExecutor` 校验 `WorkflowFactory` 冻结计划并编译分段 DPARSFA 作业，并非逐个 DAG 节点经过 `ToolRuntime`。公共状态机、冻结计划与审批要求保持一致，不应将两种内部执行方式写为完全相同。

2026-09-04 已用仓库外合成 4D 输入验证公共预处理 Worker 到 QC 的通路（删除 4 个初始 volume、保留 120 个、detrend，原始文件 SHA-256 不变）及组合 ALFF/fALFF/ReHo 通路。单会话 4D、metric-only 单主体以及阶段掩膜限制见 [验证文档](../../docs/development/mvp-verification.md)。
