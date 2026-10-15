# ADR 0044：WorkTask 步骤 readiness 检查器与收口边界

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 创建日期：2026-10-15
- 更新日期：2026-10-15
- 决策日期：2026-10-15
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[ADR 0036：持久 Work 任务与运行状态边界](0036-persistent-work-task-boundary.md)、[ADR 0039：WorkTask 权威状态重核协调器](0039-work-task-reconciliation-worker.md)、[ADR 0043：WorkTask 步骤生命周期共用转换合同](0043-shared-worktask-step-lifecycle.md)

## 背景

ADR 0043 统一了预处理与统计 `TaskStep` 的 Run 绑定和完成状态构造，并要求新步骤使用只读权威检查器。当前恢复服务仍以类型条件分支承载 readiness 判定；若新增类型继续复制这些分支，Task 状态、证据检查、任务写入和 outbox 收口会再次耦合。最近的单步骤门禁也表明，未知类型必须能以显式的“不确定”结果停止，而不能被隐式映射到既有领域。

需要规定检查器如何读取科学权威、如何表达下一步判断，以及谁有权将判断提交为持久任务状态。这个合同只支持现有两类 Run 步骤，不等同于任意工作流引擎。

## 决策

应用层定义按唯一 `TaskStep.kind` 注册的只读 `StepReadinessHandler`。注册表拒绝重复 kind；缺少 handler、步骤/Run 身份无法唯一解析、权威记录暂不可读或读取期间版本变化都返回 `indeterminate`，不得回退到默认 handler。handler 接收当前持久 Task、Step 和精确 Run ID，只通过注入的只读端口重读权威记录；不能写数据库或文件、改变 Task/Step、创建 outbox、调用模型、建立/批准 Plan、批准 QC、创建或重试 Run。

handler 返回不可变 `StepReadinessAssessment`，其中绑定 `task_id/task_version`、`step_id/step_version`、`run_id`、有限 outcome、稳定 `reason_code` 和安全的人类说明。outcome 为 `waiting_run`、`waiting_user`、`waiting_dependency`、`ready_to_finalize`、`blocked` 或 `indeterminate`，与持久 `TaskStatus`/`TaskStepStatus` 分离。等待结果可携带经过校验的 `WaitingCondition`；handler 不返回 Task/Step 更新或任意状态转换命令。

`ready_to_finalize` 表示检查器已证明进入类型专属收口所需条件，仍不代表任务已完成。它必须携带受约束的证据摘要，至少锚定稳定 Run version、冻结输入/Plan 修订及类型专属结果身份和 lineage fingerprint；不得携带模型可控制的 Artifact 来源、路径或自由执行参数。预处理检查器只在 Run 成功、权威 QC 已批准或存在明确 skip 记录、冻结目标产物和 lineage 完整匹配后给出该 outcome。统计检查器不读取或要求预处理头动 QC，但必须核对已批准并绑定当前 Run 的冻结设计，以及唯一真实、非占位的统计结果和 Artifact lineage。

应用协调层负责统一状态映射：三类 waiting outcome 使用现有版本化转换；`blocked` 仅表示确定性证据冲突并使用类型专属阻断细节；`indeterminate` 不改变任务生命周期；`ready_to_finalize` 只能进入类型专属 finalizer。协调器必须在写入前确认 assessment 的 Task/Step 版本、Run version 与冻结证据仍匹配；过期 assessment 丢弃并返回 `indeterminate`。Task 写入继续使用 CAS 和有效 Worker claim/fencing token。Task 完成与对应 proposal/result outbox 的收口仍须处于同一 Unit of Work；检查器不得接管这些写入。

事件仍只用于去重和唤醒。任务重核保留周期权威重读和 Run version 前后夹读，不能仅凭事件 payload 生成 readiness。当前无显式 `TaskStep` 的旧任务可以保留受限兼容恢复路径；显式未知、混合或多步骤任务仍按 ADR 0043 失败关闭。

首个实现注册 `preprocessing_run` 和 `statistical_run`。handler 可共用 registry 和 assessment 合同，但不得为了统一接口而合并各自的权威读取器、Artifact/结果校验器或最终 outbox 事务。

## 不在本 ADR 范围

本 ADR 不开放多步骤 DAG、并行 fanout、多分析目标身份、共享预算、自动科研决策、QC/Plan 自动审批、模型调用、任意命令或自动创建/重试 Run。将“一个 TaskStep”放宽为多步骤前，必须另行定义依赖解析、部分成功/失败、恢复身份及面向用户的原因投影。

## 后果

任务恢复有一个明确的扩展点，且检查、状态映射和持久收口的职责可分别测试。每个新步骤仍需提供自己的权威检查器、证据指纹和 finalizer；这项额外合同是避免未核验结果越过领域边界的必要成本。`indeterminate` 目前首先用于服务响应和内部诊断，不据此声称 Work UI 已展示每一种协调原因。
