# ADR 0052：CompositeGoal 阶段交接判定

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 创建日期：2026-10-20
- 决策日期：2026-10-20
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[ADR 0039：WorkTask 后台恢复](0039-work-task-reconciliation-worker.md)、[ADR 0041：分析提案续接 outbox](0041-proposal-only-analysis-continuation-outbox.md)、[ADR 0042：统计结果解释续接](0042-statistical-result-interpretation-outbox.md)、[ADR 0046：复合目标前台选择](0046-composite-goal-foreground-selection.md)、[ADR 0050：analysis-only 结果续接](0050-analysis-only-result-continuation.md)

## 背景

CompositeGoal 的进度投影已经从 root、WorkTask、Plan/outbox、统计结果和 continuation 证据计算阶段，但它把已绑定 Run 的等待压成宽泛的“等待预处理/等待分析”。proposal 与结果解释 worker 又各自决定能否继续。若仅凭 Task 或 outbox 的单一状态显示/恢复阶段，可能把过期 Plan、错绑 Run、用户审查或来源变化误认成可交接。

## 决策

### 从同一只读快照形成有限阶段决定

`CompositeGoalSnapshot` 在一个一致的只读 SQLite 快照中读取 root、会话、目标 WorkTask、Plan、绑定 Run 与其最小 QC 状态，以及两个既有 outbox 和结果 continuation。快照只携带用于身份和阶段判断的引用与状态，不复制 child lifecycle，也不替代 PlanRevision、WorkflowRun、QC、StatisticalResult 或 outbox 的权威记录。

纯 `resolve_composite_goal_handoff(snapshot)` 输出当前 CompositeGoal progress、有限阶段及一个可选的 continuation action。当前阶段限定为等待预处理、等待审查、等待已绑定 Run、等待分析、等待结果解释、等待用户、阻断、完成或待核验。只有完整快照已将目标身份、项目、Task wait、Plan/Run 引用及 outbox 关系核验一致时，才可将其细化为等待审查或等待 Run；缺失、未知或不匹配的状态保持待核验。

当前仅声明两种恢复 action：消费现有 `preprocess_then_analyze` proposal outbox，或消费现有 statistical-result interpretation outbox。action 必须指向该 root 当前精确绑定且状态为 `ready` 的 outbox。它只说明现有续接可进入其已实现消费者，不创建后继对象。

### 同一决定用于进度和模型消费者

Conversation progress 从 handoff decision 取得进度投影；两个 outbox consumer 在模型调用前读取同一决定，并核对 action 与自身 claim 的目标 ID。阶段暂不可交接时不调用模型，并沿现有 outbox 冷却/重试路径处理；proposal handoff 不就绪连续达到既有五次自动尝试上限后转为 `WAITING_USER`，保留明确原因和人工重试入口，不会无限后台轮询。worker 仍须在模型调用前后重读来源、使用原租约和 fencing token 收口；快照判定不能替代 claim、事务、版本比较或科学来源验证。

WorkTask 恢复 worker 仍从现有 Run/QC/Artifact/StatisticalResult 权威状态重核 Task。本决策不把事件 payload 作为事实，也不授权其推进 Plan 审批、QC 决定或创建 Run。

### 边界

- 支持范围仅限现有 `preprocess_then_analyze` 与 `analysis_only_external_import`，并只允许两个已实现的 outbox continuation action。
- 不自动批准或启动 Run，不重试或重建已有 Run，不自动改变科学方法、输入纳入或 QC。
- 零显著结果仍是有效统计结果；结果解释完成仍需精确的真实结果与 assistant continuation 证据。
- 不建立通用 DAG、通用状态复制表、数据库迁移或新的执行授权。

## 并发与失败边界

- 进度快照与后续 worker claim 之间存在时间差；消费者必须继续核验 claim、来源与当前权威事实。
- 两个 outbox consumer 与 WorkTask reconciler 可以并发运行。阶段决定只读，不争抢资源；唯一写入围栏仍由各自持久租约、事务与 fencing token 提供。
- stale 或过期快照不会授权模型提交。缺少/不匹配证据时不发起该阶段模型调用；消费者按既有可重试/阻断规则结束。
- `preprocess_then_analyze`、analysis-only、旧 standalone outbox 及其既有授权行为继续保持兼容。

## 验收要求

- 目标快照准确包含匹配 Task 的 Plan 与 Run/QC 事实；错项目、错 Plan、错 Run、无效 wait reference 或未知状态不能显示为等待运行/审查。
- 进度投影仅在权威依据足够时区分等待审查和等待 Run；其余阶段及完成条件继续使用原身份链。
- 两个 worker 只在 handoff action 与 claim 目标精确匹配时调用模型；阶段不一致时不调用模型，原有 fencing 和有界重试仍有效。
- proposal-ready/outbox 后续流程和零显著结果解释继续完成；重复消费不产生重复 child、Plan、Run 或 continuation。

## 后果

进度和后台续接共用一个有限的阶段解释，降低两处判断不一致的风险。每次新的目标阶段或续接类型仍须添加明确证据合同和验收，不得把当前 decision DTO 扩展为任意调度器。
