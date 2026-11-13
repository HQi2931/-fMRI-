# ADR 0039：WorkTask 权威状态重核协调器

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 决策日期：2026-10-14
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[ADR 0036：持久 Work 任务与运行状态边界](0036-persistent-work-task-boundary.md)、[Worker](../../neuroagent/workflow/README.md)

## 背景

持久 WorkTask 已能按绑定 Run 回放运行事件并重新读取 Run、QC、Artifact 和统计结果，但只有显式调用 `reconcile_waiting_run(task_id)` 时才会重核。API lifespan 不应悄悄承载本地后台循环；现有 Worker 已是共享 SQLite 的长驻进程，适合作为恢复协调的运行入口。仅依赖瞬时事件通知也会在进程停机或消费后崩溃时丢失恢复机会。

## 决策

由既有 Workflow Worker 在启动后和空闲轮询期间扫描、领取并重核有限批次的 WorkTask。Worker 不保持模型会话，不调用 Agent；扫描覆盖绑定 Workflow Run 的 `waiting_run`、绑定该 Run 的 QC 决策 `waiting_user`、统计结果登记 `waiting_dependency`，以及由 ADR 0040 明确定义目标检查器的预处理 `waiting_dependency/downstream:<run_id>`。没有目标检查器的普通 dependency 和普通科研选择等待不进入领取范围。

RuntimeEvent 作为持久唤醒提示和事件身份来源。事件回放按有界批次与任务游标读取；每次恢复都以重新读取的 Run、QC、Artifact 和 StatisticalResult 权威记录判定。启动及周期扫描即使没有未消费事件也会重读当前权威状态，用于弥补遗漏唤醒、事件已消费但生命周期更新尚未提交、以及 Worker/API 重启。

SQLite 用独立的目标索引和 WorkTask reconciliation claim 表支持领取。Claim 有独立于 Job attempt 的短租约、随机 token、轮询 cooldown 和到期后可重领。所有由持有者发起的事件消费及任务更新在同一写事务内核验 token 和有效期；过期持有者不能在新持有者接管后写入。普通用户命令继续使用既有任务版本 CAS，不持有 Claim。

协调器只更新 WorkTask 生命周期及事件回执；不得创建或重试 Run、批准 Plan、决定 QC、改变统计方法或调用模型。Job Worker 仍独立负责作业租约、attempt 和重试。统计以外的目标产物合同仅由对应的已实现检查器启用；下一步骤准备和自动角色续接仍属于后续计划阶段。

## 后果

既有 Worker 重启后可以通过 SQLite 中的任务目标恢复待核验状态，无需消息中间件或 API 内存订阅；有限扫描和 cooldown 控制 SQLite 读取频率，Claim token 则将租约从提示提升为可验证的写入 fencing。WorkTask 的索引表只记录等待类型和 Run 身份，不复制 Workflow、QC 或结果状态。

Worker 同时承担长作业和任务重核，长时间占用执行器时后续重核会延后到当前 Job 结束后的循环。若未来出现需要独立优先级、多执行节点或更严格调度延迟的需求，再评估单独协调进程；不得因此复用 Job lease 表或把事件 payload 当成权威状态。
