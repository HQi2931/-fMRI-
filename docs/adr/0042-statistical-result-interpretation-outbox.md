# ADR 0042：真实统计结果的提案专用解释续接

- 状态：Accepted
- 文档类型：ADR
- 维护者：backend_service_engineer
- 创建日期：2026-10-14
- 更新日期：2026-10-14
- 决策日期：2026-10-14
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[ADR 0033：分析 Agent 使用确定性结果事实](0033-statistical-interpretation-facts.md)、[ADR 0036：持久 Work 任务与运行状态边界](0036-persistent-work-task-boundary.md)、[ADR 0039：WorkTask 权威状态重核协调器](0039-work-task-reconciliation-worker.md)、[ADR 0041：复合任务的提案专用分析续接 outbox](0041-proposal-only-analysis-continuation-outbox.md)

## 背景

统计 WorkTask 能够在真实结果完成登记后收口，但用户此前还要再发一条消息，才能获得结果解释与报告草稿。把这一步放进统计任务 reconciler 会让确定性恢复协调器承担模型调用，也会把结果解释与预处理后分析提案的生命周期混在一起。

## 决策

为真实 StatisticalResult 增加独立的 `StatisticalResultInterpretationOutbox` 和独立消费者，不复用 `AnalysisProposalOutbox`。只有 `_complete_statistical_task` 已从服务端核验唯一结果后，才在同一 SQLite Unit of Work 中将 WorkTask 标记为 `COMPLETED` 并创建唯一 `READY` outbox。记录绑定 task、conversation、project、Run、Plan revision、result ID、bundle hash/version 和已核验 Artifact IDs；任务与结果冲突时失败关闭，不会静默复用其他任务的 continuation。结果尚未登记时仍由既有 reconciler 定期重读等待中的统计任务；reconciler 不调用模型。

消费端使用独立租约与递增 fencing token。在模型调用前和提交前都重新读取并核验会话/项目、WorkTask、Run、Plan/design、唯一 StatisticalResult、bundle hash/version 和真实 Artifact lineage。只有证据仍与 outbox 一致且消费者仍持有当前 fencing token 时，才在一个事务内追加 assistant-only continuation 并完成 outbox。模型慢返回、任务/结果变化、会话失效或旧租约均不能写入过期草稿。终态包括 `completed`、`waiting_user` 和 `blocked`；可恢复的瞬时失败使用有界退避，独立的人工重试入口按 generation 幂等。

模型只接收服务端投影的有限统计方法与聚合结果事实及确定性 readout。每个字段、列表长度、组/条件/协变量名、方向和假设文本都有上限；用户提供的标签按不可信数据处理。完整报告 JSON、subject-level 矩阵、逐 Artifact ID、路径、hash 和 lineage 不进入模型上下文。零显著结果作为正常结果解释；synthetic/mock、缺失/冲突来源及不可验证的结果不会进入模型。未知空间明确保留未知，不推断脑区或定位。

模型只能生成可审查的 assistant 解释/报告草稿。该流程不创建或批准统计 Plan，不创建或启动新的统计 Run，不修改 QC、样本纳排、检验方向、协变量或校正方法。原有统计结果及报告仍由结果服务提供，草稿不替代冻结复现报告。

`worker_main` 以独立线程和服务实例周期消费该队列；`--once` 在既有工作之外，最多分别尝试一条分析提案 outbox 和一条结果解释 outbox，并等待已领取的模型调用结束。Agent 页面在成功统计 WorkTask/Run 后刷新会话；仅在发现该独立续接仍待处理时按有限次数轮询，等待终态即停止。重试 API 使用独立状态和 generation，不与分析提案 outbox 的状态混用。

## 后果

结果解释可在统计完成后自动排队，不要求用户发“继续”；Task 完成和 READY 记录不会因单边写入而分离。额外的 SQLite 表、消费者和前端状态可增加少量维护成本，但使生命周期、并发边界和用户重试保持独立。模型输出仍需研究者审核，且该切片只覆盖真实、已登记并通过现有结果合同的统计结果；多意图目标、自动生成正式文件报告、计划审批与统计执行编排不由本 ADR 承诺。
