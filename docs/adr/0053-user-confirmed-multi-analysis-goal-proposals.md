# ADR 0053：用户确认的多分析目标拆分提案

- 状态：Proposed
- 文档类型：ADR
- 维护者：system_architect
- 创建日期：2026-10-26
- 更新日期：2026-10-26
- 决策日期：2026-10-26
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[Agent 通用自动化运行架构](../architecture/agent-automation-runtime.md)、[ADR 0041：分析提案续接 outbox](0041-proposal-only-analysis-continuation-outbox.md)、[ADR 0046：复合分析目标的前台选择与草稿隔离](0046-composite-goal-foreground-selection.md)、[ADR 0048：复合目标共享模型调用预算](0048-composite-goal-model-call-budget.md)、[ADR 0049：已有外部指标的独立统计目标](0049-analysis-only-external-import-goals.md)、[ADR 0051：analysis-only 外部来源目标的前台续聊与新建](0051-analysis-only-foreground-resume-and-new-goals.md)、[ADR 0052：CompositeGoal 阶段交接判定](0052-composite-goal-handoff-decision.md)

> 本提案尚未接受；接受前不得据此实现。

## 背景

计划 0014 阶段 B 已允许用户明确选择一个预处理 WorkTask/Run，并为同一精确来源登记多个独立分析目标。每个目标拥有自己的 CompositeGoal 身份、分析 WorkTask、proposal outbox、统计尝试及累计模型预算。当前仍需用户逐项提交请求；系统不能把一条自然语言请求拆成多个目标。

现有 `WorkAgentRouter` 每轮只选择 `PREPROCESSING`、`ANALYSIS` 或 `CLARIFY` 一个角色。`ConversationTurnCreate` 也只表达一个当前分析目标/显式来源，显式新建外部指标目标只接受一个来源。角色分类不是目标拆分器；若把多个目标塞进一个角色任务或一次 intent，就会丢失目标间独立的身份、预算、草稿和失败边界。若循环调用现有单 intent 注册入口，部分成功还会留下用户没有完整确认的目标集合。

本提案只解决“一个自然语言请求 → 多个彼此独立的分析目标”，不改变已接受 ADR 的分析方法、输入来源、审批、Run 或 WorkTask 状态合同。

## 决策提案

### 支持范围与身份

新 Work UI 只在用户明确选择已登记预处理 WorkTask 与精确 Run 后启用自动目标数量检测。检测出一个明确目标时继续现有单 intent 流程，包括该流程已支持的 queued/running/QC_REVIEW 来源登记与等待；不对单目标施加新的完整完成门禁。只有检测出两项或以上明确目标时才进入多目标提案路径，该路径要求来源满足 `WorkTask=COMPLETED`、唯一相关 `TaskStep=SUCCEEDED`、Run `SUCCEEDED`、Plan/manifest 已冻结、QC 已批准或有明确的跳过决定，并且所有目标 Artifact 与 lineage 完整且可核验。所有拆分项必须属于同一会话、项目、来源修订和已验证 Artifact lineage。

每项确认后分别建立一个现有类型的 CompositeGoal root、analysis proposal outbox 和 ADR 0048 定义的独立累计模型预算；各 root 只引用同一个已核验预处理来源，不复制其状态或产物。确认时不创建 analysis WorkTask。每个 outbox 成功生成统计提案时，沿现有路径为对应 intent 创建独立 analysis WorkTask。一个目标失败、修改或耗尽预算不会改变其他目标。Analysis role slot 一次仍只选择一个目标；批量注册不自动选中“第一个”或任何其他目标，之后沿 ADR 0046 的显式选择入口切换。

首期每个提案限制为 2–8 项，以界定单次事务和累计预算上界。不得跨预处理来源、Run、session 或项目；不得包含预处理任务、外部指标导入、混合角色、FC、重复测量或未接入的统计能力。来源未明确选择或身份不符时先阻断；检测为多目标但完成证据不足时返回来源阻断，不按“最新 Run”推断来源。单目标继续使用原有登记及等待条件，不要求预处理已完成。疑似多目标但无法可靠区分时必须澄清，不自动丢弃其余目标或注册其中一项。

### 精确预处理来源门禁

拆分请求开始前，服务端先核验 Work conversation、project、用户显式选定的 Task/Run 身份及其项目绑定。任何身份错配都在模型调用前拒绝。此后 `auto_v1` 的数量检测模型只接收本轮用户文字，不接收 MRI、subject table、manifest、Artifact/lineage 明细、Task/Run 状态或其他会话/工作区内容。

数量检测为一个明确目标时，服务端沿既有单 intent 注册/outbox 路径处理；queued、running 和 QC_REVIEW 等现有可登记状态继续等待原预处理目标，不要求先有完成产物。数量检测为两项或以上时，服务端才对精确 Task/Step/Plan/manifest/Run、QC 决定和完整 Artifact lineage 应用多目标门禁。来源需满足：WorkTask 状态为 `COMPLETED`；唯一匹配的相关 TaskStep 状态为 `SUCCEEDED`；绑定 Run 状态为 `SUCCEEDED`；冻结 Plan 与 manifest 仍是该 Task/Run 的精确输入；QC review 为 `APPROVED`，或存在身份匹配的显式 skip 决定；冻结目标 Artifact 集合完整，且每项 Artifact 的 hash、空间/类型身份与依赖 lineage 均可核验。2 项以上但未满足门禁时返回明确阻断和 source summary，不创建 child。

`source_fingerprint` 至少覆盖：Task ID/version/state；Step ID/version/state；Plan revision ID/version/hash/state；manifest revision/hash；Run ID/version/state；QC review 或 skip 决定的 ID/version/state；以及规范化目标 Artifact ID 集合、Artifact evidence hash 与 lineage hash。检测出多目标后先形成满足完整门禁的来源快照，proposal finalization 前重读 SQLite 权威状态并重核文件 evidence hash；与该快照不一致就丢弃候选。检测模型仅依赖原文，不以检测前尚未完成的来源状态作多目标准入结论。确认时在事务内重读 SQLite 身份/状态，并在提交前再次重核文件 evidence hash，必须与用户审阅的 fingerprint 完全一致。

服务端返回的 `source_summary` 区分 `single_intent_registration` 与 `multi_target_proposal`：前者按现有规则显示已登记/等待来源状态，后者仅在上述全量门禁通过时显示 eligible。Work UI 通过该服务端摘要显示 Task/Run 状态与阻断原因；用户不需新增开关，服务端仍在每次提交时重核。

SQLite 事务不能锁定或原子提交工作区文件。确认记录保存已核验的 evidence hash；每个 outbox consumer 在实际使用来源前仍须重新验证 Artifact hash 和 lineage。提交后文件变化会使该 child outbox 阻断，不得以旧 fingerprint 继续分析或重建 Artifact。

### 模型仅生成可编辑提案

新 Work UI 在用户已明确选择一个预处理 Task/Run 后，自动通过版本化客户端能力字段请求受限的 multi-intent detection；不增加用户可见的拆分开关。服务端先核验会话/项目和显式 Task/Run 身份，再按既有显式来源约束确定单个 `ANALYSIS` 角色；任何需要的路由模型调用也不得先于身份门禁。现有 Work 模型只接收本轮用户文字，用来判断并提出结构化拆分；不调用新的协调模型、不把路由小模型升级为规划器，也不允许检测模型调用注册目标或执行工具。检测为一个明确请求时沿现有单 intent 路径；清楚检测出 2–8 项时再应用多目标来源门禁并生成用户审核提案；歧义时请求澄清。旧客户端不发送能力字段，始终保留原有单目标语义。

每项至少包含顺序、模型识别出的原文区间和可编辑的 `request_text`；`awaiting_review` 状态表示所有项目都尚未确认。模型只提供候选内容，`target_id` 由服务端在验证候选之后分配；模型返回的任何 ID 都忽略，不能直接成为目标或 intent 身份。模型只能拆分用户明确表达的独立分析请求，不得补造指标、检验、对比、组别、协变量、尾部、缺失值策略、校正、阈值、纳入/排除或 QC 决定。不能从原文定位或有歧义的项目应成为澄清问题，不成为目标。服务端校验目标数、文本范围、重复项、来源和 schema；模型给出的 source、intent ID、target ID、hash、状态、审批或执行字段一律忽略。

提案和逐项编辑持久化为专用 `GoalSplitProposal` 记录，并关联原始用户消息 ID/hash。它不是科研目标，不创建或引用子 WorkTask。原始会话消息仍是请求的来源；proposal 记录保存候选拆分和用户对每一项的编辑。客户端可删除多余项、编辑顺序与请求文本，但不能新增目标或更换已选择的来源；修改后仍须保留 2–8 项。删至不足两项的 PATCH 整体拒绝，保留原提案，提示用户另行提交单目标请求，不自动注册剩余项。成功编辑递增 revision、重新生成 hash，并清除本地逐项勾选，使用户重新逐项检查当前列表。提案卡挂在产生它的 assistant turn 下；响应同时返回服务器核验的 source summary。单目标响应摘要可显示现有登记/等待状态；检测到多目标但来源未达完成/QC/Artifact 准入时，返回 summary 和阻断原因，不创建任何 child。

批量确认时，应用服务为每个保留项建立不可变 `ChildIntentAuthority`。该 authority 将一个 intent 精确绑定到 `proposal_id`、`proposal_revision`、`proposal_hash`、原 `source_message_id`/`source_request_hash`、服务端分配的 `target_id`、最终 `request_text`/`request_hash`、`source_fingerprint` 和确认幂等请求摘要。确认审计按 child 保存这些身份字段及确认的会话版本；后续读取不得用“最新消息”、展示标题或模型生成 ID 替代此映射。注册后的 proposal 与 authority 不可再编辑；需要不同请求必须重新创建 proposal 和 child intent。

child proposal outbox 通过该 authority 按 `target_id`/`intent_task_id` 获取其精确已确认目标请求，并同时核对 CompositeGoal root、proposal/outbox 身份和精确来源。该 resolver 必须覆盖创建准入及每次使用：扩展 `application._outbox_intent_matches`、repository `persistence._analysis_outbox_source_matches`、后台 `verify_analysis_outbox_source` 与 admitted-request 重核，并让 CompositeGoal summary 的 `_verified_analysis_summary` 只从精确的 authority 读取对应目标文本。每个 consumer 通过自身 intent ID 唯一定位 authority，重核 proposal/revision/hash、来源消息、target/text hash、source fingerprint 和 root/outbox 绑定；不得把一个 child 的请求文本用于另一个 child，也不得退回“最新消息”。只增加 root/outbox 行而不扩展这些准入、后台验证和摘要读取点，多个 outbox 将无法通过现有来源校验或可能显示错目标文本，因而不得上线。

在明确批量确认之前，提案路径不得创建 CompositeGoal、child-intent authority、目标 WorkTask/TaskStep、analysis outbox、Plan、Approval、Run、QC 决定或运行授权；不得写入任一 child 的 active intent/analysis role slot，也不得启动 outbox consumer。提案失败、超时、拒绝或停留待确认均只留下对话/提案审计数据，不产生目标。

### Proposal DTO、哈希与状态

为支持页面刷新后的读取/编辑及可核验的 target↔intent 关系，新增 Alembic migration，使用规范化记录而不是只把所有目标塞进一个 JSON blob：

- `goal_split_proposals` 保存 proposal、创建/最后编辑时的会话版本审计、来源身份、source message ID/hash、revision/hash、状态和幂等关联；历史会话版本不作为内容授权条件。
- `goal_split_proposal_targets` 每项目标一行，保存服务端分配的 `target_id`、顺序、原文区间和当前可编辑文本/hash；用 `(proposal_id, target_id)` 唯一约束。
- `goal_split_child_intents` 只在批量确认时创建，每行是不可变的 child-intent authority 和 target↔intent/root/outbox/budget 绑定；至少唯一约束 `intent_task_id` 和 `(proposal_id, target_id)`，并以外键引用 proposal/target 与目标记录。

`goal_split_child_intents` 同时保存注册审计的原消息身份、来源 fingerprint、proposal revision/hash、最终文本/hash、会话版本、确认幂等请求摘要和注册回执。注册后 proposal target 与 authority 均不可编辑；需要变化时创建新的 proposal。若降级可能删除已注册 authority 或无法表达现有 target↔intent 关系，migration downgrade 必须拒绝，而不是丢失绑定。下面的 DTO 表示 API 投影字段：

| 字段 | 含义 |
| --- | --- |
| `proposal_id` | 服务端生成的不可变提案身份 |
| `conversation_id`、`project_id` | 会话及项目边界 |
| `source_preprocessing_task_id`、`source_run_id` | 用户显式选定的预处理 Task/Run |
| `source_summary` | 服务端核验的 Task/Step/Plan/manifest/Run/QC 状态、Artifact/lineage 完整性、`eligible` 和阻断原因；供 UI 展示，不代替确认时重核 |
| `source_fingerprint` | 服务端核验的 Task/Step/Plan/manifest/Run 版本与状态、QC 决定、目标 Artifact evidence hash 与 lineage hash |
| `source_message_id`、`source_request_hash` | 已持久化的原始用户消息 ID/hash；不在提案或 outbox 中重复存储整段原文 |
| `conversation_version` | 查询/命令响应读取的当前会话版本，供下一条命令 CAS 使用，不进入 proposal hash |
| `created_conversation_version`、`last_edited_conversation_version` | 提案创建和最后编辑时实际提交的会话版本，仅用于审计 |
| `revision` | 提案内容修订号；文本、顺序或目标集合编辑后递增 |
| `targets[]` | 服务端分配的 target ID、有序原文区间、可编辑请求文本及其 hash |
| `proposal_hash` | 下述规范化完整提案的 SHA-256 |
| `status` | `awaiting_review`、`stale`、`registered`、`rejected` 或 `expired` |
| `registered_children[]` | 从规范化 child-intent binding 投影；仅 `registered` 后包含 target ID、intent ID、CompositeGoal root ID、outbox ID 与 budget ID；此时尚无 analysis WorkTask ID |

`proposal_hash` 由服务端对规范化 JSON 计算，至少覆盖 `proposal_id`、会话/项目身份、提案修订号、来源 fingerprint、原消息 ID/hash、按顺序排列的服务端 `target_id`、最终 `request_text` 与其 hash。不覆盖当前 Conversation version、可变展示文案、模型置信度或 Provider 元数据。目标文本、顺序、增删、精确来源、原消息身份或提案修订变化都会使旧 hash 不可确认；其它目标的消息或显示角色变化不改变该内容身份。每个 child 的 intent ID 和 request hash 在注册时由服务端按提案、修订、target ID、最终目标文本及精确来源确定，不接受模型指定。

`awaiting_review` 是唯一可编辑/确认状态。精确来源、原消息或相关会话/项目/工作区/权限绑定无法再满足原合同则使旧确认失效，并按原因进入 `stale`；明确取消进入 `rejected`，到期进入 `expired`。单纯 Conversation version CAS 冲突不改变提案状态或 hash。只有所有目标原子注册成功后才转为 `registered`。Provider/数据库暂时失败不伪造 `registered`，也不改变任何 child；原请求结果未知时以原键原请求重放或查询回执。

### API 边界

在现有 `POST /conversations/{conversation_id}/turns` 增加仅用于兼容的版本化客户端能力字段，例如 `goal_split_detection: "auto_v1"`。新 Work UI 在用户已经明确选定 `preprocessing_task_id` 与其精确 `target_run_id` 时自动携带该字段；它不是用户可见开关，也不能由模型设置。服务端先核验 Work conversation、project 和精确 Task/Run 绑定，再在 `Work AUTO + auto_v1` 下只用本轮用户文字检测目标数量：一个明确请求继续现有单 intent 注册/outbox 路径（包括 queued/running/QC_REVIEW 等等待状态）；2–8 个明确请求只有通过完整来源门禁才返回 proposal-only 审核卡，否则返回 source summary/阻断；歧义返回澄清。缺省该字段的旧客户端请求永远沿用旧单 intent 语义，不根据文本自动拆分。`start_new_analysis_goal` 和 `external_analysis_import_id` 不与检测混用。

新增 conversation-scoped 读写入口：

| 方法与路径 | 请求/响应要点 | 语义 |
| --- | --- | --- |
| `GET /conversations/{id}/goal-split-proposals/{proposal_id}` | 返回 `GoalSplitProposalView`、当前 `conversation_version` 和重新核验的来源资格 | 只读取该 Work 会话内精确 proposal；不跨会话或项目，不递增版本 |
| `PATCH /conversations/{id}/goal-split-proposals/{proposal_id}` | `{expected_version, expected_revision, proposal_hash, targets[]}` → 新 `GoalSplitProposalView` | 保存逐项编辑/删除/排序；要求 `Idempotency-Key`，重算 hash 并清除逐项确认 |
| `POST /conversations/{id}/goal-split-proposals/{proposal_id}/confirm` | `{expected_version, proposal_revision, proposal_hash, confirmed_target_ids[]}` → `GoalSplitRegistrationView` | `confirmed_target_ids` 必须与当前所有保留目标 ID 完全相同；要求 `Idempotency-Key`；成功后返回 N 组 intent/root/outbox/budget ID，不含 analysis WorkTask ID |

当前 turn 的 assistant payload 只携带 proposal ID、hash、revision、来源摘要和目标编辑视图。确认端点不接受目标文本修改；若需修改，先 PATCH 并审阅新 hash。API 不提供“自动确认”字段或由模型触发的确认动作。

用户在每个目标行单独勾选确认；客户端只在最终 `POST .../confirm` 中提交完整的 `confirmed_target_ids[]`。这些尚未提交的勾选不是服务端授权状态，刷新页面后用户需重新审阅并勾选；服务端只在批量确认事务成功时把该逐项确认集写入不可变 authority/审计。

### Proposal turn、内容身份与命令并发

多目标 proposal-only 分支按以下顺序持久化，避免模型生成之前没有可核验的 source message 身份：

1. `POST /conversations/{id}/turns` 先在幂等 admission 的 `SqliteUnitOfWork` 中校验 Work 会话、项目及显式 Task/Run 选择，分配并持久化真实 user message ID、正文 hash 和幂等请求摘要；Conversation version 随用户消息递增。新 Work UI 在用户已选择 Task/Run 后自动携带 `goal_split_detection="auto_v1"`；旧客户端缺省该字段。此时不创建 `GoalSplitProposal`、child authority、CompositeGoal、WorkTask、outbox、Plan 或 Run。
2. admission 提交后再次核对 Work/项目、原消息及显式 Task/Run 身份。请求带 `auto_v1` 时，在既有有界 Work turn 预算和外发策略下，只用原请求文字调用检测模型；这些 ID/hash 只在服务端绑定，不进入模型输入。此处不要求来源已完成，也不调用工具。省略 capability 的旧请求沿用旧路径，不执行检测。
3. 检测出一个明确请求时，服务端走既有单 intent finalization，包括 queued/running/QC_REVIEW 来源的登记和等待。检测出 2–8 项时才应用完整完成/QC/Artifact 门禁；不足则返回已核验 source summary 与阻断，不创建 proposal 或 child。通过时形成来源 fingerprint，在 proposal finalization 的 `SqliteUnitOfWork` 中重核 admission、原消息、Work/项目及相关来源/权限绑定，并核对完整来源快照。相关身份或证据变化就丢弃候选；仅发生无关后台消息不丢弃。服务端分配 target IDs，在当前会话版本上原子追加 assistant 卡片、保存 proposal/source summary 与幂等响应，记录实际提交的创建版本并计算不含该版本的 `proposal_hash`。歧义、超范围或无效结构只澄清。

同一 turn key 的成功响应丢失重放返回已保存响应，不再次调用模型或创建 proposal。未完成 admission 的重放复用同一 source message ID/hash，不重复追加用户消息。finalization 在当前 SQLite 事务内重读并 CAS 会话版本；写入竞争可沿原有有界事务重试重核相同候选，不重新调用模型，也不跳过相关身份核验。模型将来若读取额外上下文，须把这些实际依赖纳入失效条件，不能沿用仅原文依赖的假设。PATCH 在同一事务核对当前 `expected_version`、proposal revision/hash、资格与原消息身份；成功编辑递增 proposal revision 和 Conversation version，记录最后编辑版本，重算内容 hash 并清除勾选。

无关 assistant continuation、其它目标进展或仅改变显示上下文的角色选择，不修改本提案 hash 或状态。命令的 `expected_version` 仍须等于提交时当前会话版本；不匹配返回可识别的版本冲突且零注册写入。客户端刷新 GET 后，仅当 proposal ID/revision/hash、逐项显示内容、来源 fingerprint 和当前有效资格都相同时，可在当前页面保留勾选，由用户再次点击提交；不得自动重发确认。整页重载仍清除未提交勾选。提案内容编辑、相关来源/项目/工作区/权限变化、退出 Work、取消、过期或状态变化都使旧确认失效；显示角色变化本身不隐式更换提案明确绑定的来源。

更新 `expected_version` 的新命令必须使用新幂等键。若前次结果未知，先用原键原 payload 重放或查询注册回执，确定结果后再决定是否提交新命令。成功重放先解析已保存幂等响应，不因后续消息递增版本而失败。此合同只适用于本提案的消息独立输入和确认；不改变其它 Work 草稿/运行入口既有的版本与授权要求。

### 原子注册、预算与重放

新确认请求必须同时匹配当前会话的 `expected_version`、proposal 的 `proposal_revision`、服务端重算的 `proposal_hash`、当前 `awaiting_review` 状态、精确来源 fingerprint 及逐项确认 ID，并保持当前相关身份/权限有效；不要求创建或最后编辑版本等于当前会话版本。批量确认使用共享 `SqliteUnitOfWork`，事务内重读 Work conversation、project、source message、Task/Step/Plan/manifest/Run、QC、Artifact/lineage 和 authority 身份，并重核 SQLite 版本/状态与文件 evidence hash。共享来源按同一完整快照核验一次，N 项分别绑定该快照，不能因共享而省略任何 child 的身份校验。来源变化、重复/范围外目标、少确认、版本冲突或确认集合不一致均使整批零写入。

在同一 SQLite Unit of Work 中，应用服务为 N 项逐项写入：不可变 ChildIntentAuthority、独立 CompositeGoal root、已验证来源对应的独立 `READY` analysis proposal outbox、独立 ADR 0048 budget、逐 child 确认审计、proposal 的 `registered` 状态和完整幂等响应。若现有 registration 路径要求单独的 ready transition，该 transition 也必须在同一事务中完成。审计至少绑定 `proposal_id/revision/hash`、原 `source_message_id/hash`、服务端 `target_id`、最终文本及 `request_hash`、`source_fingerprint`、`expected_version`、请求幂等键摘要和最终 intent/root/outbox IDs。共享来源只作为经验证的引用；消费者只能在整个事务提交后领取 READY outbox。

确认时不创建 analysis WorkTask。outbox 成功生成并原子提交该 child 的统计 proposal 后，沿现有 proposal outbox 路径创建对应的独立 analysis WorkTask；outbox 失败时仍保留已注册 root/outbox 与其自身状态，但不伪造 WorkTask 或统计提案。任何一项 authority、root、outbox、budget、审计、幂等响应或 proposal 状态写入失败，事务整体回滚；不得留下部分 SQLite 记录或前台指针。后台 consumer 只能在事务提交后看到 outbox。该入口不创建统计 Plan、不审批、不创建 Run、不启动 MATLAB/DPABI；后续每个 outbox 沿现有 proposal-only 审查边界运行。

SQLite 原子性不覆盖原始/产物文件系统。若事务期间或提交后发现 Artifact 文件 hash 已改变，必须失败关闭；若文件在提交后变化，consumer 的来源重核会阻断使用，不能回写为源通过或继续分析。

提案生成使用已有有界 Work turn 调用预算和数据外发策略，不使用 child CompositeGoal 预算，也不增加协调模型。确认成功后，每个 root 按 ADR 0048 独立获得现有预算上限并独立预留/结算；一个 child 的模型调用不扣其他 child 的预算。N 份预算意味着批次总上界随目标数增长，因此首期限为 8 项并在 UI 展示每项独立额度；若需要跨 child 聚合预算，必须另立 ADR，不得隐式共享 TaskBudget、root 或 outbox 状态。

成功提交将 proposal、全部 child 记录和该确认的幂等响应一同持久化。确认用 `status=awaiting_review`、proposal revision/hash 和 conversation version 做 CAS，状态只允许在全部记录成功时原子转为 `registered`。相同 `Idempotency-Key` 与相同规范化请求重放原 N 组 ID，不重复注册；同 key 改变版本/hash/确认集合返回 `409 idempotency_key_reused`；并发处理中沿用 `409 idempotency_request_in_progress`。若同一 proposal 被不同幂等键并发确认，CAS 仅允许一个事务创建记录；败方重读后，若已注册回执与相同 proposal revision/hash、来源 fingerprint 和确认目标集完全一致，则返回既有注册回执；否则返回冲突。败方不得二次写入 child。

任何失败均使当前注册事务回滚，无部分 root/outbox/budget 或幂等成功响应。单纯会话 CAS 冲突保留 `awaiting_review`；相关来源/身份变化按独立 stale 收口，取消/到期保持相应状态。提交后响应丢失时原键原 payload 返回原响应。已注册 proposal 换新 key 也只能返回精确匹配的既有回执或冲突，不能再次注册或夺回前台。authority 保存实际确认版本供审计；消费者验证其不可变内容与当前精确来源，不要求当前会话版本等于历史确认版本。

### 失败与并发

- `auto_v1` 请求未显式选来源或会话/项目/Task/Run 身份错误：检测模型调用和注册均为零。身份正确但来源未完成时仍可做纯文本数量检测；单目标沿原登记路径等待，多目标返回来源阻断，不创建 proposal 或 child。
- `auto_v1` 检测的模型 JSON 无效、目标无法回链原文、猜测科研参数、混合角色或拆分歧义：不创建 child，也不退回现有单目标注册；请求澄清或让用户改写。省略 `auto_v1` 的旧客户端请求才完全沿用原有单目标路径。
- 模型生成期间原消息、相关身份/权限或来源快照变化：不保存旧提案；仅其它目标的消息改变会话版本则在重核后可保存，不重复调用模型。
- 无关后台消息引起确认 CAS 冲突：零注册写入，提案不变；GET 核验相同内容与来源后可保留当前页面勾选，用户新点击用新 key 提交；不得自动确认。
- proposal 编辑期间并发修改：按 `expected_version`、`expected_revision` 与 `proposal_hash` 拒绝过期 PATCH，不覆盖较新编辑。
- 批量确认期间任一身份或来源不一致：全部回滚；不得逐项部分成功或把剩余项自动当作单目标注册。
- confirmed batch 已提交但客户端未收到响应：原幂等键返回原 child IDs；重复 worker/HTTP 重放不得增加 root/outbox 或重置预算。
- 同会话其他目标的后台 continuation 不得更新本批任一未选中的 Analysis role slot；后续选择依 ADR 0046 完成。

## 替代方案

1. **让现有 WorkAgentRouter 返回多个角色/目标。** 不采用。router 的责任是单轮角色选择；混合目标分解需要目标文本、来源绑定、逐项审查和批量注册，不应挤入单角色分类合同或使用路由小模型的只读预算。
2. **服务端循环调用现有单 intent endpoint N 次。** 不采用。会产生 N 次独立幂等操作，无法保证全有或全无，也可能在后续项失败时留下未经完整确认的 child。
3. **创建一个 CompositeGoal/WorkTask，再用多个 TaskStep 表示分析。** 不采用。多个分析请求是独立科研目标，各自有输入补充、计划、运行、预算和失败状态；跨目标关系不属于 `TaskStep`，也不引入通用 DAG。阶段判定仍限 ADR 0052 的已注册 continuation action。
4. **一个 root 关联多个 analysis outbox。** 不采用。会合并不同提案 hash、目标前台身份和预算，破坏 ADR 0046/0048 已建立的每目标隔离。
5. **模型直接注册全部目标或在用户确认前生成统计 Plan。** 不采用。模型输出没有注册/审批权限；自动创建方案会越过用户对每项科研请求的逐项审查。
6. **要求用户把每项请求重新发送为独立消息。** 安全但保留现有交互缺口；只作为拆分歧义、来源不支持或模型提案失败时的人工补救方式。

## 实施阶段

1. **确定性提案与 child-intent authority 合同**：建立 proposal、规范化 target 和 target↔intent authority migration；定义服务端 target ID、内容 hash、来源 fingerprint、错误码与逐 child 审计。先身份校验，再用原文检测数量，仅多目标施加完整来源门禁；缺省能力字段保持旧语义。内容 hash 不含会话版本，创建/编辑/确认版本仅审计；命令版本仍 CAS。扩展并验证 `application._outbox_intent_matches`、repository `persistence._analysis_outbox_source_matches`、后台 `verify_analysis_outbox_source`/admitted-request 重核和 summary `_verified_analysis_summary`，统一按精确 authority 读取已确认文本。验证 admission→模型→相关身份重核→原子保存顺序，区分无关消息与相关来源变化。该切片不创建目标 WorkTask 或 outbox。
2. **逐项审阅体验**：用户明确选择 Task/Run 后，新 Work UI 自动请求检测，不增加多目标模式开关。界面在对应 assistant turn 下展示已核验 source summary 和 2–8 个目标；每项可编辑、排序、删除并单独勾选确认。成功编辑后刷新 proposal revision/hash 与 Conversation version，清除本地确认态；不接受新增目标或删至不足两项。通过持久 proposal/target 表支持刷新后 GET、PATCH 和继续审阅；注册后的 target 与 child authority 不可编辑。来源未达到完成/QC/Artifact 准入时展示 API 返回的 summary 与阻断原因，不让前端自行推断是否可用。
3. **原子批量注册**：实现确认 application use case、幂等和共享 `SqliteUnitOfWork`。事务内重核完整来源与 conversation/proposal 身份，并为每项目标建立 ChildIntentAuthority、独立 CompositeGoal root、proposal outbox 和预算；同时提交确认审计、proposal `registered` 状态和幂等响应。事务全部成功或全部回滚；外部文件不属于 SQLite 原子范围，必须以证据 hash 固定并在消费者中再核验。确认不创建 analysis WorkTask、Plan 或 Run。
4. **后台 fanout 核验**：扩展现有 outbox 输入读取以按精确 child authority 读取被确认的目标请求；复用同一预处理 source 的已验证 allowlist。确认事务将来源已完成的 N 个 outbox 注册为 READY，但只在提交后允许 consumer 领取。验证每项续接只提交到自身 attempt、继续使用既有 fencing 与 per-intent 预算；成功生成统计 proposal 时才沿现有路径建立独立 analysis WorkTask；不增加跨目标 dispatcher。
5. **文档与验收收口**：更新 API、计划 0014 和 MVP 验证事实；只有在确定性故障/重放验证完成后才标记实现。真实 Provider 或 MATLAB/DPABI 验收仍须依既有授权流程单独进行。

## 验收要求

- 新 UI 显式选择预处理 Task/Run 后携带 `goal_split_detection="auto_v1"`；未选来源或身份错配时模型调用与注册为零。身份正确的 queued/running/QC_REVIEW 来源可以执行仅原文的数量检测：单目标继续原登记/等待路径；2–8 项只有在 Task `COMPLETED`、step/Run `SUCCEEDED`、冻结 Plan/manifest、QC approved/explicit skip 与完整 Artifact/lineage 均通过时才形成审阅提案，否则只返回来源阻断。歧义只澄清；旧客户端完全保持原语义。
- router 始终输出一个角色；拆分模型只输出结构化草案。对未确认 proposal，数据库没有 child CompositeGoal、WorkTask/TaskStep、outbox、Plan、Approval、Run、QC 决定或 active analysis pointer 变化。
- user message ID/hash 在模型前持久化；模型返回后重核相关身份、消息与来源。无关 continuation 在生成期间改变会话版本不导致重复模型调用或丢弃有效候选；提交仍在当前事务 CAS。GET/PATCH 支持刷新后读取/编辑有效提案。
- 其它目标后台完成引起旧确认 CAS 冲突时，批次零写入且 proposal hash/status 不变；GET 相同内容/来源且资格有效后可保留当前页面勾选，必须由用户新点击、新 key 提交。再次发生竞争仍返回冲突，不自动注册；整页重载清除勾选。
- 编辑文本/顺序/集合使 revision/hash 变化并清除确认；项目、Work 模式、相关来源/工作区/权限变化、取消或到期即使 hash 相同也拒绝。注册成功响应丢失且之后会话版本变化时，原 key 原 payload 返回原 N 项回执；改 expected_version 却复用原 key 返回冲突。注册后普通聊天不使 authority 失效，来源漂移仍阻断消费者。
- 每个提案目标能回链原始用户消息；target ID 只由服务端分配。用户可逐项编辑、排序、删除并分别确认。任一编辑改变 hash、增加 revision/version 并使旧确认失效。确认审计绑定 proposal id/revision/hash、source message id/hash、target id、最终文本/hash 和 source fingerprint。
- outbox 准入、后台 source 验证/admitted-request 复核和 CompositeGoal summary 文本读取都按精确 target↔intent authority 验证；错误 child、同会话其他目标或旧消息都不能供此 outbox/summary 使用。
- 少一个目标确认、确认 ID 重复/多余、proposal hash/revision 过期、conversation version 竞态、source fingerprint 改变、越项目/越会话或 outbox 身份不匹配均使批次零写入。
- 批量注册成功后恰有 N 个不同 child authority、intent/root、READY proposal outbox 和预算；确认时没有 analysis WorkTask。仅提交后 consumer 可领取 outbox；每个 outbox 成功生成统计 proposal 后才建立其独立 analysis WorkTask。它们共享同一精确预处理来源引用但没有共同可变生命周期。每个目标 budget/reservation 独立，前台 Analysis role slot 不被任意 child 抢占。
- 同 key 同请求重放返回同一 N 组 IDs；同 key 改请求冲突；同 proposal 不同 key 并发由 `awaiting_review→registered` CAS 防重，败方只返回精确既有回执或冲突。共享 `SqliteUnitOfWork` 的任一故障都不留部分 authority/root/outbox/budget/proposal/幂等响应；文件系统变化由 evidence hash 检测并由 consumer 再核，不能声称与 SQLite 原子。
- outbox consumer 仅在批量事务提交后工作；proposal 阶段和确认阶段都不创建统计 Plan/Approval/Run，不批准 QC、不启动 MATLAB/DPABI。
- 现有单分析 intent、`analysis_only_external_import`、预处理目标、ADR 0046 前台恢复、ADR 0048 预算、ADR 0049 来源绑定与 ADR 0052 两种 handoff action 保持兼容。

## 后果

此提案将自然语言拆分限定为“模型准备、用户逐项编辑/确认、服务端原子注册”，并复用现有每目标状态与 outbox，不引入通用多目标调度。代价是需要持久化 proposal 修订/哈希、扩展每 child 的请求身份读取，并在同一 SQLite 事务内提交 N 组记录。目标数上限用于约束一次确认的事务与累计预算范围；独立目标预算仍按现有 ADR 0048 运行。
