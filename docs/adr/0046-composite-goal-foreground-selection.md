# ADR 0046：复合分析目标的前台选择与草稿隔离

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 创建日期：2026-10-17
- 更新日期：2026-10-17
- 决策日期：2026-10-17
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[Agent 通用自动化运行架构方案](../architecture/agent-automation-runtime.md)、[ADR 0028：Work 角色任务引用与确认作用域](0028-work-task-bindings.md)、[ADR 0041：分析提案续接 outbox](0041-proposal-only-analysis-continuation-outbox.md)、[ADR 0045：WorkTask 有界线性步骤调度](0045-bounded-linear-worktask-step-scheduling.md)

## 背景

多个显式分析目标可以共享同一个精确的预处理来源，各自拥有 CompositeGoal、分析提案 outbox 和统计 WorkTask。会话仍只有一个 ANALYSIS 角色任务槽；后台续接会更新该槽，按角色取最新消息的草稿读取也可能采用另一个目标的内容。用户因此无法可靠切回较早的目标继续补充科研选择。

仅恢复角色任务槽不足以解决问题：后续后台消息仍可能成为全会话最新分析草稿。另一个风险是两个独立目标具有相同的 proposal hash；仅用 conversation ID 和 proposal hash 的统计冻结请求不能区分切换前后的目标。

## 决策

### 使用现有角色任务槽保存选择身份

为 `ConversationWorkTaskView` 增加可选 `analysis_intent_task_id`，持久化于现有 `ConversationRow.work_tasks_json`。它表示该分析角色槽当前承接的复合目标，不复制 CompositeGoal、WorkTask 或 outbox 生命周期；不新增表、列或数据库迁移。旧会话缺少该字段时保留原有兼容行为，不从最新文本猜测历史目标身份。

前台提交分析草稿时，服务端依据已核验的草稿身份同步该指针；明确成为普通分析草稿时清除该指针。指针不能由模型的任意输出单独赋予权威，仍须核对目标、会话、项目、原请求 hash 和精确来源。角色切换保留各角色任务引用；切换回分析角色不重新选择另一个目标。

分析草稿读取依据该指针过滤 `analysis_draft`，只采用相同 intent 的持久消息。补充科研选择时使用的 consumed envelope、原请求及来源引用必须与该草稿身份一致，不允许另一目标较晚的 continuation 覆盖当前上下文。前端的当前分析草稿也应使用同一选择范围，避免服务器与页面分别选择不同的“最新草稿”。

### 选择入口仅恢复尚未冻结的目标

增加 conversation-scoped 的显式选择用例，以 intent ID 寻址，要求会话乐观版本及独立 HTTP 幂等键。应用服务重新读取并核验 WORK 会话、project、目标、分析请求 hash、proposal outbox、预处理来源及当前分析 attempt；首个切片仅接受需要用户处理、且当前分析尚未冻结 Plan 或绑定 Run 的目标。不存在可核验草稿、目标冲突或超出该范围时拒绝，不回退到另一个目标。

恢复从该 intent 最新的精确 `analysis_draft` 消息开始。草稿必须通过结构与内容 hash 核验；最新草稿不完整时只恢复其 draft hash 和 intent 指针，清除旧 proposal、Plan、Run 与统计 WorkTask 的角色槽引用，不复活历史提案。最新消息具有完整 proposal 时，还须匹配目标当前 attempt、proposal hash、draft hash 与精确来源，才恢复原始 `proposal_message_id` 和统计 WorkTask 引用。不得只凭 CompositeGoal 的旧 active task 恢复较早的完整提案。

选择在现有 Unit of Work 内原子更新分析角色槽、foreground role、active run 投影和审计记录。不复制或重发 proposal，不伪造 user message，不创建 Plan、Approval 或 Run。响应丢失后的相同幂等请求返回已保存响应，不重新选择前台，也不覆盖后来选择的目标。

### 后台续接保存结果并尊重前台指针

分析 outbox 消费者继续在模型调用前后核验来源，在事务外调用模型，以原有 fencing token 将 assistant continuation、分析 WorkTask/attempt 关联和 outbox 终态原子提交。

会话已保存分析 intent 指针时，只有相同 intent 的后台续接可以更新分析角色槽及其前台投影。不同 intent 的续接仍保存自身消息、任务和 outbox 结果，但不得更改已选目标、foreground role 或 active run。未选择目标的旧会话保留兼容行为。后台消息先后次序不能替代用户明确选择。

### 统计冻结请求绑定原始提案消息

`StatisticalWorkContext` 增加可选 `proposal_message_id`；复合目标的统计冻结请求必须明确携带该字段，服务端核对它与当前角色槽及原始 assistant proposal 消息完全一致。非复合旧请求保持兼容；调用方提供消息 ID 时也必须核验，不能忽略。

冻结 Plan 的服务端 `work_context` 继续保存已核验的 proposal message ID、proposal hash 和 draft hash。切换前发出的迟到请求即使与新目标具有同一 proposal hash，也不能冻结新目标。恢复上下文不重新签发或恢复旧执行确认；Plan 校验、审批、一次性运行授权、QC 决定及实际 Run 身份继续由既有权威记录与版本检查控制。

## 并发与失败边界

- 选择与后台续接均重读当前 conversation version；过期选择或模型准备结果不能覆盖后续用户决定。
- 选择状态、角色引用、审计和幂等响应在同一事务提交；故障不得留下部分恢复。消费者继续使用已有 claim fencing，不因前台选择放宽 outbox 提交资格。
- 相同来源、相同研究文本或相同 proposal hash 不代表相同 intent；身份必须逐项核验，歧义时拒绝。
- 较新不完整草稿使旧提案失去前台确认资格；只读历史消息仍保留供审计，不作为恢复授权。
- 首个切片不恢复已有 Plan/Run 的目标，也不改变 CompositeGoal 的完成条件或 ADR 0045 的步骤调度合同。

## 替代方案

独立选择表或新 conversation 列可提供显式关系约束，但现有角色 JSON 已能表达单个当前分析目标，首个切片不增加存储结构。通过复制 assistant 提案使其成为最新消息会改变原消息身份，容易误复活旧确认，因此不采用。仅在浏览器保存选择不能跨刷新、进程重启或后台提交保持一致，也不作为权威。

## 验收要求

- 两个目标共享来源且 proposal hash 相同时，选择恢复各自原始消息和草稿；切换前的迟到统计冻结请求被拒绝。
- 选择 A 后 B 后台完成，A 的草稿、角色槽、foreground role 和 active run 不被覆盖，B 的 continuation、任务与 outbox 正常保存；刷新和重启后保持同一选择。
- 目标最新消息是不完整修订时，不恢复其旧 proposal 或确认资格；错会话、错项目、hash 冲突、非当前 attempt 和缺失来源均拒绝。
- conversation version 竞态、outbox fencing 失效、事务内故障及幂等重放不产生重复提案、部分恢复或重新抢占前台。
- 已有 Plan/Run 的选择请求明确拒绝；旧无选择会话、普通分析及角色切换维持兼容，既有科研审批和执行门禁仍有效。

## 后果

该决策使已有多分析目标可以逐步获得可恢复的前台交互，且不建立另一套任务状态。代价是所有当前分析草稿及关联上下文读取必须遵守同一 intent 指针，并为统计冻结请求补充消息级身份。实现与验证进度由计划 0014 和验证记录维护；本 ADR 接受该边界，不表示已有 Plan/Run 恢复或通用目标 dispatch 已经实现。
