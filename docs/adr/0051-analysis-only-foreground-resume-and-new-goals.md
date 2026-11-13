# ADR 0051：analysis-only 外部来源目标的前台续聊与新建

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 创建日期：2026-10-18
- 更新日期：2026-10-18
- 决策日期：2026-10-18
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[ADR 0046：复合分析目标的前台选择与草稿隔离](0046-composite-goal-foreground-selection.md)、[ADR 0047：冻结分析目标的精确上下文恢复](0047-frozen-analysis-goal-restoration.md)、[ADR 0048：复合目标共享模型调用预算](0048-composite-goal-model-call-budget.md)、[ADR 0049：已有外部指标的独立统计目标](0049-analysis-only-external-import-goals.md)、[ADR 0050：analysis-only 外部来源目标的结果续接](0050-analysis-only-result-continuation.md)

## 背景

ADR 0049 与 ADR 0050 已定义 analysis-only 外部来源目标的持久根、累计预算、结果续接和进度投影。用户仍需要能在同一 Work 会话中选择一个已有目标、继续补充草稿，或明确开始一个新的独立目标。若每条补充消息都重新从消息正文和来源创建 root，预算、原始请求身份和冻结上下文就会分叉；若把新来源隐式套到已选择 root 上，则可能把科研决策和结果串到错误目标。

本决策规定前台续聊和显式新建如何沿用现有 Analysis role slot、CompositeGoal 与来源身份合同。运行时实现和验证记录见[计划 0014](../plans/0014-general-agent-automation.md)及[MVP 验证记录](../development/mvp-verification.md)。

## 决策

### 权威来源与不变量

- 当前前台目标以 Work conversation `work_tasks_json` 中的 Analysis role slot 指针为准；请求里的 `analysis_intent_task_id` 仅断言这个指针，浏览器缓存、最新消息和模型输出都不能替代它。切换目标仍由既有 goal select API 持久完成。
- root 的 `intent_task_id`、`goal_kind`、原始请求 hash 和冻结来源 revision 是目标身份事实；预算及 reservation 账本是累计模型用量事实。统计 WorkTask、Plan、Run、StatisticalResult、outbox 与 assistant 消息继续各自作为任务、授权和结果事实来源，不复制到 root。
- 当前外部纳入记录是来源是否仍有效及其 revision 的权威。root 只绑定它建立时核验过的 revision；续聊或新建时 revision 不一致即拒绝，不能从最新来源状态悄悄改写已绑定身份。
- Conversation version 与 HTTP 幂等键分别限定前台并发提交和新建重放。版本冲突不提交回复；Work 回合的幂等键以摘要持久化。升级前的原始 key 记录会先与旧版请求序列化摘要核对，再在同 key 重放时改存摘要并按当前摘要查找；不匹配时不改写请求身份。新建 root ID 锚定 conversation 和 key 摘要，原始请求与来源 revision 仍作为不可变身份事实保存在 root；相同 key 若携带不同身份事实，会命中同一 root 并冲突。

### 现有目标选择仍由目标选择用例完成

选择既有目标继续使用 ADR 0046/0047 定义的 conversation-scoped goal select API；不在自然语言解析或来源选择器中暗中切换目标。选择时读取并核验 Work conversation、项目、目标 root、Analysis role slot 和来源 revision，在同一版本边界内更新前台目标及必要的角色引用。已冻结 Plan/Run 的选择遵守 ADR 0047，只恢复精确的只读上下文，不重新打开草稿、不创建新的分析 attempt，也不授予审批或执行权限。

计划中的 `ConversationTurnCreate` 增加可选字段 `analysis_intent_task_id`，其语义是对**当前 Analysis role slot 指针**的断言，不是目标选择命令。若字段缺省，服务读取当前 role slot；若提供，则必须与该 slot 当前指向完全相同。缺失、过期或不匹配时在模型调用前拒绝请求，不能回退到最新消息或另一个目标。只有 role slot 当前引用 `analysis_only_external_import` root 时，本 ADR 的 analysis-only 续聊规则适用；普通无来源分析与 `preprocess_then_analyze` 保留各自路径。

选择请求与回复最终提交均须核验捕获的 Conversation version、Analysis role pointer、CompositeGoal identity 以及来源 revision。若模型调用期间用户切换目标、会话版本变化或来源 revision 改变，迟到回复不得绑定到新目标或写入当前会话；拒绝提交并保留已记入的模型预算消耗。

### 补充消息续接同一 root

当 `start_new_analysis_goal=false`（包括缺省值）时，且当前 Analysis role slot 指向一个可修改的 analysis-only root，前台补充继续该 root。补充消息是对原请求的上下文补充，不替换 root 的 `original_request_hash`，不创建新 intent，也不重置累计模型预算。服务复用该 root 已持久化的模型预算账本；预算记录缺失或身份不匹配时失败关闭，不能调用模型或创建一份新预算。

补充请求可省略来源 ID。若包含来源 ID，必须与 root 绑定的外部纳入 ID 以及已核验来源 revision 完全一致。缺省来源只表示继续已绑定的来源，不能成为从当前页面选中项推断另一个来源的依据；不匹配来源必须在模型调用前拒绝。改变来源需显式开始新目标。

冻结 Plan/Run 上的自然语言回合沿 ADR 0047 的只读恢复合同处理。请求中的来源引用只作为身份断言，不会把冻结目标转成可修改草稿；需改变方法、参数或来源时，必须明确建立另一个目标，并仍经过既有统计 Plan 校验、用户审查、Approval 和 Run 门禁。

### 显式新建独立目标

`ConversationTurnCreate` 还增加默认值为 `false` 的 `start_new_analysis_goal`。只有 Work AUTO 回合、用户明确给出且服务端确认仍为当前项目已纳入状态的单个外部指标来源，才允许设为 `true`。它开始一个独立 analysis-only root，并把新的 root 选为该 Analysis role slot 的当前目标；不支持的模式、缺失来源、非当前项目来源或已变化的纳入 revision 均在首次模型调用前拒绝。若同时提供 `analysis_intent_task_id`，视为互相冲突的命令并拒绝；新建请求不借用旧目标 ID。

新 intent ID 由 goal kind、conversation ID 和 HTTP `Idempotency-Key` 的 SHA-256 摘要生成。project ID、用户原始请求 hash、外部来源 ID 及其完整来源 revision 作为不可变身份事实保存在 root，并在相同 intent ID 重用时逐项比对；完整来源 revision 至少覆盖 ADR 0049 定义的导入版本、source-tree、inclusion、manifest 与组合 revision hash。原始幂等键不持久化。相同幂等键和相同身份事实重放为同一新建操作；相同键搭配不同请求或来源身份必须冲突。root 一经创建，后续失败也不得释放这项 key-to-root 绑定。新建前后均重核来源与会话版本。

相同正文和来源使用不同幂等键时允许得到不同 intent ID，因此可创建两个独立目标；意图身份中的唯一性只体现在已存在的 CompositeGoal `intent_task_id`，不得把幂等键写进 root 的来源、请求或预算字段。创建/选择和首个模型请求之间若 role pointer 已被另一回合改变，当前回合不能覆盖后来选择。

### 持久状态与授权边界

分析续聊共享原 root 的唯一累计预算；首个模型调用前须能读取、核验并预留预算。预算缺失、耗尽、重复 reservation 或预算提交冲突均失败关闭，不能通过重建 root 或改用前台临时预算恢复额度。用户补充文本不会改写既有 root 请求 hash；显式新建才产生新的 identity hash 和预算账本。

本切片不增加 SQLite 表或列，也不迁移既有 root、attempt、预算或 reservation。CompositeGoal 已有的 intent ID 是不透明键，显式新建请求用 key 摘要稳定寻址该 ID，身份事实仍保存在既有 root 字段；会话仍使用 ADR 0046/0047 的 Analysis role slot。不得把 Plan、Run、StatisticalResult、WorkTask 或 outbox 状态复制到 root。

执行授权保持不变：新建或续聊都不能绕过统计草稿校验、冻结 Plan、用户审查、显式 Approval、Run 创建、QC 决策或结果证据核验。普通无来源分析、`preprocess_then_analyze`、已冻结目标的只读恢复以及历史 standalone 结果解释保持兼容。

## 并发与失败边界

- role slot 指针不是模型推断出来的目标身份；每一轮以服务端选择记录为准，并通过版本/身份核验阻止跨目标写入。
- 来源 ID 缺省只在续接已选 root 时有效。显式新建必须提供一个已纳入且仍匹配的来源；来源变化要求拒绝，不能自动创建新 revision 的 root。
- 相同 `Idempotency-Key` 只有在 conversation、project、原始请求 hash 和来源 revision 全部一致时才幂等；原始 key 不进入持久化数据。
- 模型调用成功但前台选择已变化时，回复不提交到新前台；已经发生的模型费用仍计入原 root 预算。
- 若同一显式新建请求在创建 root 后发生响应丢失，重放应解析为既有 root，不增加 root 或重置预算；若响应之前用户已另选目标，重放不得静默抢占前台。

## 替代方案

每条消息都创建新 root 会把补充文本误当成新的科研目标，并使模型预算和结果关联碎片化。将来源 ID 直接用作选择指令会允许来源控件绕过明确的目标选择。把当前 intent 存在浏览器中不能解决重启、多个客户端及后台前台竞争，也不能作为服务端权威。新增数据库列保存 nonce 或另建目标表会重复表达已有的 intent ID 和角色槽，无需为本切片增加存储状态。

## 验收要求

- 当前选中的 analysis-only root 在补充消息前后保持同一 intent ID、原 request hash、绑定来源 revision 与累计预算；缺少预算时模型调用次数为零。
- 省略来源 ID或提供与 root 精确匹配的来源 ID 均续接原 root；提供其他来源 ID、过期的 `analysis_intent_task_id`、错 conversation/project 时在模型调用前拒绝。
- 对同一正文、来源、会话和项目，重复 `Idempotency-Key` 只建立一个 root；不同 key 建立不同 root；同 key 改变正文或来源时返回冲突；存储中不存在原始幂等键。
- 显式新建只接受 Work AUTO 与当前项目已纳入的单个外部来源。首次调用前来源版本再次核验，回合成功后新 root 成为选中目标；无来源普通分析和 `preprocess_then_analyze` 继续走既有路径。
- 在模型调用期间切换目标或更改会话版本/来源 revision 时，迟到回答不得修改新选中的 root、草稿、消息或进度；预算账本仍保留已消耗/预留额度。
- 已冻结 Plan/Run 只能恢复只读上下文，任何补充都不会重开统计设计、建立 Plan、Approval 或 Run；显式新建仍走现有 Plan review、Approval、Run 和 QC 门禁。
- SQLite schema 与既有 root/attempt/budget/reservation 记录无需迁移，目标选择、对话提交和幂等重放的事务故障路径均不产生重复 root、部分指针或预算重置。

## 后果

该合同使一个已选分析目标可以跨消息继续，并允许用户用独立幂等键明确开启相同正文的新目标；同时让目标选择、来源身份、预算和迟到回复的拒绝由持久服务端身份控制。代价是前台回合需要维护版本快照，并在模型调用前后重核 role pointer、root 和外部导入 revision。该切片已按本 ADR 实现并通过定向验证；Accepted 描述架构决策状态，不表示计划 0014 的其他自动化阶段已经完成。
