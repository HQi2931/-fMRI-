# ADR 0047：冻结分析目标的精确上下文恢复

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 创建日期：2026-10-17
- 更新日期：2026-10-17
- 决策日期：2026-10-17
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[Agent 通用自动化运行架构方案](../architecture/agent-automation-runtime.md)、[ADR 0028：Work 角色任务引用与确认作用域](0028-work-task-bindings.md)、[ADR 0046：复合分析目标的前台选择与草稿隔离](0046-composite-goal-foreground-selection.md)、[ADR 0042：统计结果解释续接](0042-statistical-result-interpretation-outbox.md)

## 背景

ADR 0046 建立了持久的分析目标选择指针，并把首个恢复切片限制为尚未冻结 Plan 或绑定 Run 的草稿。用户切换到其他目标后，原目标的统计设计、运行和结果仍然存在，但不能通过该入口恢复其前台上下文。

冻结后不能再从任意最新完整提案推导当前设计。统计 Plan 已把原始 proposal message ID、proposal hash 和 draft hash 写入不可变 `work_context`，统计 WorkTask 与 TaskStep 也保存精确 Plan/Run 引用。恢复必须核验这条身份链，而不能重新创建设计、重发提案或把选中目标等同于执行确认。

## 决策

本 ADR 扩展 ADR 0046 的选择范围，使当前 active analysis attempt 已冻结 Plan、已绑定 Run 或已经完成的分析目标可以恢复上下文；ADR 0046 的未冻结草稿恢复、后台隔离和消息级统计请求身份规则继续有效。继续使用现有角色任务 JSON 中的 `analysis_intent_task_id`，不增加表、列或数据库迁移，不创建新的任务生命周期。

### 以冻结 Plan 的原始提案消息为恢复依据

选择入口先解析 conversation-scoped CompositeGoal 的当前 active analysis attempt 和统计 WorkTask。存在冻结 Plan 时，以该 Plan 的 `work_context.proposal_message_id` 找回原始 assistant 提案消息；恢复其原始消息 ID、proposal/draft hash、精确 WorkTask ID、Plan ID 和已存在的 Run ID。恢复不追加提案副本，不按相同 proposal hash 搜索其他目标，也不依据浏览器缓存、项目最新 Run 或最近完整消息猜测引用。

提交前须共同核验：

- WORK 会话、项目、目标 intent ID、原请求 hash、active attempt 和 WorkTask 身份一致；目标当前 attempt 的 proposal hash 与原消息、统计步骤相同。
- WorkTask 只有受支持的单个统计步骤；根与 TaskStep 的 conversation、intent、request hash、draft hash、Plan ID 和存在时的 Run ID 完全一致。步骤序号、类型及绑定必须符合现有统计任务合同。
- 原消息确属该会话中的 assistant/analysis 消息，包含结构有效且 hash 匹配的草稿和提案，其 intent、原请求 hash、项目、预处理来源 Run 和 manifest 与目标 outbox 一致。
- Plan 属于该项目且类型为 `statistical_design`；`work_context` 的 conversation、proposal message ID、proposal hash 和 draft hash 与上述对象完全一致。冻结 design、校正及相关来源绑定须与原提案一致；Plan 完整内容、manifest 和 environment 的计算 hash 必须等于保存的 Plan hash。
- 已绑定 Run 时，Run 必须属于相同项目并引用该精确 Plan，与 WorkTask 根及 TaskStep 的 Run 绑定相同。其他目标的相同 hash、同项目的其他 Run 或历史 attempt 均不能替代。

原始消息缺失、历史记录不足以证明身份、引用冲突或出现未知状态组合时拒绝恢复，不修补或改写冻结记录。未绑定 Plan 的目标继续使用 ADR 0046 的最新精确草稿规则。

### 较新草稿变更不能复活旧方案

恢复冻结上下文时，仍检查同一 intent 的较新真实草稿消息。较新草稿的内容 hash 与冻结 draft hash 不同，或其来源/目标身份冲突时，拒绝把旧 Plan 恢复为当前可操作方案；不能跳过不完整修订去恢复较早完整提案。只读消息明确标记 `analysis_draft_unchanged` 的副本不算新修订，也不替换冻结 Plan 指向的原始 proposal message ID。

选择已经完成的目标不重新打开其草稿或创建新的分析 attempt。需要改变科研方法、参数或来源时应走明确的新目标或既有修订用例，不能借只读结果查询隐式改写完成任务。

### 读取历史证据与授权写操作保持边界

恢复仅更新会话的分析角色任务引用、foreground role 和 active run 投影，并保存审计及幂等响应。已绑定 Run 时，active run 必须是该目标的统计 Run；未绑定 Run 时不伪造运行。恢复不修改 Plan、Approval、WorkTask、Run、QC 或 outbox 生命周期，不签发确认，不创建、启动或重试运行，不调用模型或 MATLAB。

历史设计和结果的上下文恢复只要求冻结身份与完整性成立，不要求当前机器的 MATLAB、SPM、DPABI 或图谱安装仍符合旧环境。实际校验、审批和启动继续使用现有来源、文件、图谱、环境、Plan 状态与版本门禁，选择不能覆盖这些阻断。

前端复用原统计提案的设计/运行恢复入口读取已经存在的 Plan 和 Run。Plan 仍为 draft、awaiting approval 或 approved 时，按现有入口进行明确的校验、审批或启动；有效的历史 Approval 保留其原权威，但恢复动作本身不构成新的执行授权。存在 Run 时只恢复和查看该 Run，不能再次提供建立同一运行的操作；失败后的恢复和重试仍使用原 Run 的专属入口。

冻结对象加载失败时保留其绑定身份并允许重新读取，不能退化为“建立新设计”或“再次启动”。切换目标后，迟到的设计、运行或结果响应不得改变另一个目标的前台状态。

### 已完成目标的自然语言只读查询保持身份

完成目标的 envelope 可以是 `completed`，不能依赖仅识别 `consumed` envelope 的草稿续写逻辑恢复上下文。自然语言查询结果、进度、方法或报告时，保留原 intent 指针、draft/proposal message、Plan、Run 和任务引用；只读回复不得清空目标身份、重新生成 proposal 或把任务变成可修改草稿。

所有结果事实、只读工具和确定性结果查询必须绑定选中的统计 Run，且复查会话项目与 Plan 身份。缺少或冲突的统计 Run 引用时失败关闭，不退化为项目级全量结果查询，也不读取当前预处理角色或其他目标的最新 Run。结果解释、报告及其异步续接仍通过原有真实结果证据合同核验；选择不把 mock、synthetic 或不完整结果提升为真实科研结果，零显著结果仍是有效结果。

### 事务和并发

选择沿用 conversation version 与 HTTP 幂等键。目标、Task/Step、Plan、Run 和原消息的身份核验与前台引用写入在一致的 Unit of Work 内收口；并发绑定 Run、切换 active attempt 或修订草稿使旧选择失败，不能提交部分恢复。幂等重放返回已保存响应，不重新抢占当前目标。

后台其他 intent 的提案或结果解释仍可保存自己的消息与 outbox 结果，但不得改变已选目标及其 active run。只读查询不改变冻结 hash 或执行确认，已消费授权和旧确认不得因切换目标而重新生效。

## 替代方案

只放宽旧选择入口的状态条件会遗漏冻结消息、Plan 和 Run 的反向核验，因此不采用。复制冻结提案为新消息会破坏原 `work_context` 的消息身份，也可能重新触发设计建立。为历史目标另设一套可变审批/运行投影会与现有权威记录竞争，现有引用恢复已能满足该范围，无需新增状态机。

## 验收要求

- draft、awaiting approval 和 approved 的冻结设计可恢复原始消息、Plan 和角色引用；选择不新增 Plan、Approval 或 Run。
- 运行中、等待结果及已完成目标恢复精确统计 Run；结果查询、模型事实和工具权限均限制到该 Run，不受其他目标或预处理角色影响。
- 相同 proposal hash 的不同 intent、旧 attempt、原消息错配、Task/Step 引用冲突、Plan 内容 hash 不符和跨 Plan/项目 Run 均拒绝。
- 较新不完整草稿或真实修改阻止旧 Plan 恢复；unchanged 只读快照不改变原消息身份；完成目标查询后再次刷新、选择及读取仍保持相同目标。
- 并发草稿修订、attempt 切换、Run 绑定、事务内故障及幂等重放不留下部分恢复、不重复启动，也不恢复已消费授权。
- 当前环境变化不阻止合法历史读取；实际审批和启动仍因过期来源或环境被既有门禁拒绝。
- 前端绑定对象加载失败时不显示新建替代操作；切换会话/目标后的迟到响应不显示另一目标的设计、Run 或结果。

## 后果

用户可以通过同一目标选择入口回到既有设计、运行和结果，同时保留冻结审批与科研来源的可审计性。代价是恢复用例必须核验完整身份链，并让完成目标的自然语言读取与草稿修订路径保持明确边界。实施和验证状态由计划 0014 与验证记录维护，本决策不扩展多步骤任务、任意历史 attempt 恢复或通用目标 dispatch。
