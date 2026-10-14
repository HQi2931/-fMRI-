# API v1 契约

- 状态：Implementing
- 文档类型：API
- 维护者：backend_service_engineer
- 更新日期：2026-10-10
- 关联：[系统设计](../architecture/system-design.md)

所有业务接口位于 `/api/v1`。当前路由的机器事实来源是 [OpenAPI 文档](../../web/openapi.json)；运行中的同一契约可从 `/api/v1/openapi.json` 读取。

## 通用规则

### Work 角色任务引用

会话返回 `foreground_agent_role` 和 `work_tasks`，后者分别保存角色的草稿哈希、当前提案消息/哈希、正式方案与运行引用；`active_run_id` 表示前台角色的运行。旧 Work 会话默认预处理。

`PATCH /conversations/{conversation_id}/work-agent` 接受 `{agent_role, expected_version}` 和幂等键，版本冲突拒绝切换；只适用于 Work，保留其他角色任务。Work 的 `POST /statistical-designs` 传入 `work_context={conversation_id, proposal_hash}`，服务端比对冻结提案并保存绑定。校验、审批与启动时复查前台角色和当前草稿；过期提案或另一角色的确认返回 409。独立统计客户端可继续省略 `work_context`。统计方案/运行与会话引用在同一事务保存，幂等重放不重复建任务或夺回前台角色。

同一 Work 统计方案已有绑定运行时，新幂等键的重复启动返回 `statistics_work_run_already_created` 和已有运行编号；相同幂等键仍重放原响应。运行失败后的恢复继续使用现有 Worker 入口。

显式 `preprocess_then_analyze` 目标的预处理产物全部核验通过后，独立 outbox Worker 会自动准备 proposal-only 分析草稿，并仅追加 assistant continuation；该过程不会生成伪造的用户消息，也不会批准或运行统计方案。`POST /conversations/{conversation_id}/analysis-outbox/{intent_task_id}/retry` 接受 `Idempotency-Key`，只重试该会话内可恢复的 `WAITING_USER` outbox；相同幂等键重放原响应，不可恢复的证据冲突返回业务错误。重试只重新排队草稿准备，不会确认统计方法或启动运行。

### Work 参数编辑与 T1 预处理

Work 体验优化新增以下接口（阶段实现，完整交互验收见计划 0012）：

- `POST /conversations/{conversation_id}/work-confirmation`：接受 `{command, expected_draft_hash?}`，需幂等键；只接受服务端签发的选项，复用原有授权路径，不调用模型理解启动意图。版本或选项变化返回冲突。
- 确认接口使用独立于普通回合的幂等作用域，绑定完整请求体；相同请求与键先返回已保存响应，再对新操作检查版本。响应正文解码失败时客户端保留原键，重试不创建第二个运行。批准、运行创建与挑战消费在同一事务内提交。
- `POST /conversations/{conversation_id}/work-review`：接受 `{base_hash}`，需幂等键；重新准备当前完整方案或更新启动确认，不直接创建运行。若工作区输入修订改变，持久化返回失效后的草稿、需重新核实的字段及起点重确认选项；撤回旧事实和确认，保留用户选择。相同幂等键可重放恢复响应。
- `GET /artifacts/{artifact_id}/content` 与 `/preview.png`：按注册产物编号下载或生成有限 NIfTI 预览，检查路径边界、文件大小和内容哈希；预览不替代空间配准或科学 QC。
- `GET /runs/{run_id}/qc-review`：返回此运行最新的持久化 QC 审查记录，尚无记录时返回 `null`，运行不存在时返回 404。页面不依赖事件摘要里的编号查找 QC；事件或方案读取失败不遮蔽已加载的结果与下载入口。

候选编译使用独立 `proposal` 来源，不等同于用户批准。草稿保存可自动编译完整候选，`compiled` 状态由实际校验结果决定；启动仍须显式确认。来源、事实、版本和字段状态保存在结构化草稿中，普通回复不再递归展开内部字段。

正常预处理将 T1 准备、配准、分割/DARTEL 与指标计算合并到一次运行，不提供中途准备审查接口。

- `PATCH /conversations/{conversation_id}/work-draft` 接受 `{base_hash, patch}`，返回 `ConversationTurnView`。必须提供幂等键；更新当前 Work/工作区草稿并在条件满足时编译候选，不调用模型、批准或运行。未知字段和来源/确认字段拒绝；数值或依赖问题保存在 `draft_issues`，问题未解决时不提供确认请求。过期哈希返回 `work_draft_changed`（409）。
- `work_draft.sources` 按点分字段路径保存来源，用于区分待审查建议、用户修改与数据事实；不代替科研来源与审批证据。变更后旧确认失效，候选须重新校验和编译。

Work assistant 消息的 payload 可包含 `confirmation_request: {title, summary, options: [{label, description, command}]}`。这是协调器生成的展示契约；用户显式选择后将 `command` 原样提交到上述确认接口。当前草稿版本未变且尚未使用时，方法问答后的页面可恢复与该版本绑定的确认；参数变化、工作区切换、编译失效或已消费时不恢复旧启动操作。选项数量为一至三个；缺项草稿不签发确认。启动 command 包含绑定方案/QC 的一次性 challenge，服务端继续校验有效期、作用域和单次消费。普通回答文本不能生成按钮。确认实现见 [ADR 0022](../adr/0022-work-review-and-direct-confirmation.md)，历史设计见 [ADR 0018](../adr/0018-work-confirmation-dialog.md)。

Work 回合 payload 可包含 `work_timings: {stages: [{stage, duration_ms, calls}], note}`，只记录实际发生的阶段；嵌套耗时可能重叠，不应相加。计时不进入冻结输入与科学参数。`questions` 是服务端过滤后的待决定问题，`recommendation_reason` 是可展示的模型建议理由。参数已填写但与读取证据冲突时，不将相关问题当作已解决。

工作区 JSON 读取结果另附 `numeric_metadata`，仅包含本次请求行范围内、类型和数值有效的 `RepetitionTime` 与不超过 100 项的 `SliceTiming`。它由本机解析器提取，仍经过出站策略，不能携带身份字段或任意字符串。正文省略敏感行导致 JSON 片段无法解析时，服务端可使用这些独立数值证据推导采集事实；原始敏感正文不恢复。

工作区文本修改提案使用原文件哈希绑定审查内容；新建文件只能包含一个完整内容块，父目录必须存在。构建与应用补丁均拒绝默认排除目录、越界路径、符号链接及影像写入。应用前原文件变化返回冲突，部分写入失败时恢复原文件并移除本次新增文件，不把失败更新报告为成功。

规划回合携带页面明确选择的 `selected_input_stage` 时，若输入阶段尚未冻结且该候选可选，服务端签发对应起点确认入口，不依赖模型是否填写阶段建议。用户仍须明确确认，之后复查候选并冻结全量证据；此操作不启动计算。Segment 标准化未指定不同结构像输入时复用已选 T1 引用，显式不同引用或 `null` 不被覆盖。

- JSON 业务写入 `POST` 请求要求 `Idempotency-Key` 头。key 的作用域包含具体操作；同一作用域内，相同 key 只能绑定规范化后的同一请求体。multipart PDF 上传与显式论文索引是例外；索引按稳定 chunk ID upsert，可手动重试。上传时客户端在响应不确定时应先按 SHA-256/文献列表核对，不要盲目重复上传。
- 修改已有资源时，请求体会携带 `expected_*_version`、计划哈希或 revision 哈希。不匹配时失败关闭，不自动覆盖。
- 业务错误、请求校验、框架 HTTP 错误、未匹配 API 路径和意外 `500` 都返回 `{"error": {"code", "message", "details", "trace_id"}}`；意外错误不会返回原始异常文本。每个 HTTP 响应都有 `X-Trace-ID`，客户端仍应先检查 HTTP 状态码。
- Artifact 列表返回元数据、相对路径、校验和与 provenance；下载和预览通过上述按产物编号验证的接口提供。

幂等请求按以下规则恢复：

| 服务端记录 | 响应/行为 | 客户端要求 |
| --- | --- | --- |
| 首次请求或已过期的 `pending` 租约 | 当前调用取得有时限的处理所有权；外部 Provider 调用期间由所有者心跳续租。任何过期记录都可能被一个调用原子接管，系统无法仅凭租约区分进程崩溃与心跳延迟 | 请求内容不变时继续使用原 key，不要通过换 key 猜测前次是否提交 |
| `pending` 且租约仍有效 | `409 idempotency_request_in_progress` | 稍后使用相同 key 和完全相同的请求重试 |
| `completed` | 返回已保存的原响应，不重复业务写入或 Provider 调用 | 将其视为原请求结果，随后可清除本地 pending key |
| 相同 key、不同请求 | `409 idempotency_key_reused` | 不得用该 key 提交改变后的请求；修正请求后使用新 key |

网络中断、取消、`5xx`、`idempotency_request_in_progress`、`idempotency_lease_lost`、`idempotency_race` 或 `idempotency_completion_conflict` 都不能证明服务端或远端 Provider 未接受请求，客户端必须保留原 key。浏览器仅在当前标签页的 `sessionStorage` 保存“请求指纹 → 随机 key”，不保存请求体；成功或明确的业务 `4xx` 后清除对应记录。续租与所有权围栏会降低并发接管和重复业务写入风险，但不能保证已经发往远端的请求不会被计费或执行。

## 系统和项目

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | API 和数据库健康状态 |
| `GET` | `/environment/probe` | MATLAB、SPM、DPABI 与适配器环境锁摘要 |
| `POST` | `/projects` | 创建项目并登记允许的源/工作根目录 |
| `GET` | `/projects` | 列出项目 |
| `GET` | `/projects/{project_id}` | 读取项目 |
| `GET` | `/projects/{project_id}/audit-events` | 按单调游标读取项目审计事件 |
| `POST` | `/workspaces/check` | 对用户选择的本机目录执行只读 DPABI/fMRI 格式检查 |
| `POST` | `/workspaces/pick` | 打开服务所在 Windows 会话的系统文件夹选择器 |
| `POST` | `/projects/{project_id}/datasets` | 在项目允许根内登记数据集 |

`POST /workspaces/check` 对 DPABI-ready 目录返回 `stage_candidates`、`stage_selection_required`、
`rule_version` 和每个候选的受试者/文件覆盖数。`selected_input_stage` 只能由用户在确认候选后提交；
服务端会重新扫描并绑定工作区路径、候选检查令牌和版本化阶段规则，生成冻结 manifest。即使候选只有
`FunRaw` 或 `FunImg`，也不能省略确认；模型的阶段建议仅作为待审字段，不能直接写入请求。确认成功后，
后续 `prepare_preprocessing_plan` 只把选定阶段放入 `functional_files`，其余 checkpoint 留在 inventory。

## 持久化对话与工具编排

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/conversations` | 创建 `chat` 或 `work` 对话并持久化欢迎消息 |
| `GET` | `/conversations?mode=...` | 按更新时间读取对话、消息和工具调用记录 |
| `GET` | `/conversations/{conversation_id}` | 恢复一段完整多轮对话 |
| `POST` | `/conversations/{conversation_id}/turns` | 保存一轮消息，执行本地工具或经脱敏策略调用 Chat 模型 |
| `POST` | `/conversations/{conversation_id}/statistical-result-interpretations/{task_id}/retry` | 对当前会话中可恢复的统计结果解释续接进行幂等重试，返回更新后的对话 |

结果解释重试使用 `Idempotency-Key` 请求头，不需要请求正文。服务端要求 task 属于该 conversation，且 outbox 为可恢复的 `waiting_user`；不支持的错误类别返回 `result_interpretation_not_recoverable`。重试只将原 outbox 的新 generation 重新排队并追加 assistant 状态消息，不创建或批准统计设计，也不创建或启动 Run。续接状态通过会话 assistant message payload 的 `statistical_result_interpretation` 或 `statistical_result_interpretation_retry` discriminator 呈现；该状态独立于 `analysis_outbox_*`。仅 `retry_available=true` 的当前 generation 显示重试操作。完整证据边界见 [ADR 0042](../adr/0042-statistical-result-interpretation-outbox.md)。

Work 回合请求可带 `agent_role: "preprocessing" | "analysis" | null`。省略或传 `null` 时由服务端根据当前任务和最近交接记录路由；显式角色用于界面手动切换。响应的 assistant payload 保存 `agent_role`、角色显示名和该角色独立草稿，工具审计行也记录角色。旧的预处理草稿默认归属 `preprocessing`。

`chat` 先调用本地 `rag_rsfmri_question`；存在模型配置时，将限定范围的问题和本地证据通过
`OutboundContextPolicy` 脱敏后交给所选 LLM。请求中的 `allow_remote_search=true` 只会路由到
声明 `web_search` 能力的 Profile，并将 URL 引用、模型 Profile、上下文哈希和用量摘要保存到
`rsfmri_chat_llm` 工具记录。没有模型配置且未要求联网时保留纯本地 RAG 回退。

当所选 Profile 声明 `tool_use` 时，`chat` 知识问答改走**有界工具调用循环**：模型可连续调用
`search_evidence`（文献与项目文档检索）与 `list_skills`，服务端逐步返回工具结果，最多受步数、
墙钟与 token 三重预算约束。循环产出的回答仍经过与单次路径相同的引用校验；循环未产出答案
（预算耗尽或 Provider 失败）时自动回退单次路径，因此响应不会因循环未完成而变空。回复元数据
中的 `chat.metadata.tool_loop` 记录 `enabled`、`stop_reason`、`steps`、`tool_names` 与逐步审计记录。

## 工具调用审计记录

`ConversationToolCallView` 在单次工具调用之外还承载有界 Tool Loop 的步进信息：

| 字段 | 含义 |
| --- | --- |
| `tool_name` / `status` / `input` / `output` / `error` | 与既有单次记录相同 |
| `step_index` | 循环内的步序号，从 1 开始；单次工具调用与循环前写入的历史行为 `null` |
| `risk_tier` | 工具权威层级：`read` / `propose`；`execute` 只可能来自服务端动作，绝不会是模型发起的工具调用 |
| `duration_ms` | 本次工具调用耗时 |

`GET /conversations/{id}` 的 `tool_calls` 按 `created_at`、`step_index`、`tool_call_id` 排序，因此
循环步进的展示顺序等于执行顺序。`POST .../turns` 的响应同时提供 `tool_call`（单次记录）与
`tool_calls`（本次循环步进，可能为空）。

工具状态包含 `rejected`，用于保存未获允许的工具请求及其诊断；拒绝不会授予执行权限，也不会阻断后续有效回合的持久化。

`work` 回合由服务端确定性协调器解析为闭集 `ConversationAction`，再调用既有应用服务。客户端不再提交 `card_kind`，服务端也不暴露卡片能力目录、任意组件名、脚本或执行代码。缺少参数的请求返回待确认工具记录和明确缺项；解释或未知请求只返回说明，不产生业务写入。

当前自然语言捷径识别同时包含 DemoData、Sub_001 和 ALFF 的请求。它只接受已选择的独立工作副本，冻结单受试者 manifest，并创建固定的兼容性冒烟计划：TR 2.0 秒、234 时间点、Slice Timing 奇数层后偶数层且参考层 33、Realign、0.01–0.08 Hz raw ALFF 与 DPABI 自动掩膜，其余处理关闭。响应保存 Project、Dataset、Manifest、SkillPlan 和 PlanRevision；服务端随后为每个 QC 选择签发短时一次性的 `confirmation_id`，只有原样回传并通过方案/项目/对话绑定校验的挑战才会批准并排队 MATLAB 作业。`real_execution_confirmed` 仅作为兼容字段，不能单独授权。

显式 Work `action` 仍作为类型化 API 层保留，其行为如下：

| action | 行为与边界 |
| --- | --- |
| `setup_workspace` | 在用户选择的目录上显式创建项目、数据集并冻结当前文件清单；DPABI-ready 工作区必须携带已确认的 `selected_input_stage`；Work 不检查或整理目录格式，路径/权限等技术错误仍失败；要求项目名、数据集名和工作根目录，不使用隐式名称 |
| `prepare_preprocessing_plan` | 使用完整 `skill_plan_intent` 和 `expected_project_version` 调用现有 Skill Resolver；缺少会改变科研含义的参数时返回待确认，不猜测默认值 |
| `preview_preprocessing_run` | 在启动前重新校验计划哈希与审批状态、冻结输入、环境锁、工作区访问和磁盘余量；只返回技术预检结果，不做目录格式判断或排队 |
| `start_preprocessing` | 从已审批且当前有效的计划排队 MATLAB 预处理；要求当前对话签发且未消费的 `confirmation_id`、精确计划 ID/哈希和 QC 绑定；持久化工具名为 `start_dpabi_preprocessing` |
| `get_run_progress` | 读取目标或活动运行的服务端状态 |
| `create_head_motion_qc` | 对已完成且处于 `qc_review` 的运行补建或重建头动 QC 草稿；正常路径由 Worker 成功钩子自动触发 |
| `get_qc_status` | 按 `qc_review_id` 或活动运行只读查询 QC revision，并校验其项目/运行上下文 |
| `approve_qc_review` | 在用户明确批准时提交精确 QC revision version、run version 和 review hash |
| `skip_qc` | 在已完成计算且等待 QC 的运行上记录用户明确的跳过决定，并继续后续流程 |
| `get_statistical_results` | 按当前项目及可选目标运行列出已登记统计结果 |

项目/数据集/manifest 建立、计划编译和预检都复用既有应用服务及其路径、版本和谱系校验。真实启动服务只负责排队，实际 MATLAB 仍由现有 Worker 执行。浏览器在存在活动运行时定期读取 `/runs/{run_id}` 并显示阶段、进度和 attempt；轮询不创建额外对话消息或工具调用。

### 模型可选择的 Work 动作是上表的严格子集

上表的 `action` 是**服务端与 UI 的入口**，不等于模型可以选择的动作。模型可选项的唯一事实来源是
`neuroagent/application/work_actions.py`，且只包含只读与提案两类：

| 模型动作 | 层级 | 含义 |
| --- | --- | --- |
| `conversation` | read | 纯对话回答，不触发任何操作 |
| `get_progress` | read | 读取运行状态 |
| `get_qc_status` | read | 读取 QC 状态 |
| `get_statistical_results` | read | 读取已登记统计结果 |
| `prepare_statistical_analysis` | propose | 更新独立的统计设计草稿，不创建批准或运行 |
| `request_preprocessing_handoff` | propose | 记录分析所缺的预处理产物与服务端数据引用，交由预处理 Agent 续接 |
| `prepare_preprocessing_plan` | propose | 合并本轮参数草稿并展示审阅 |
| `propose_run_confirmation` | propose | 仅展示需要用户本人回复的启动确认指令 |

`setup_workspace`、`start_preprocessing`、`create_head_motion_qc`、`approve_qc_review`、`skip_qc`
属于服务端专用动作：它们只能由服务端校验的请求或用户原样回传一次性精确确认指令触发。模型输出这些名字
会**直接通不过 `WorkInterpretation` 的结构校验**，即模型在词汇层面就不具备提出执行的能力。
未连接模型时的关键词回退路径 `resolve_action()` 同样只能返回只读或提案动作。

### Work 的只读工具

声明 `tool_use` 的 Profile 在 Work 中可先读取服务端状态再作答。`work_context` 已携带运行/QC/方案
状态，因此这里只补充它没有的内容，且全部为只读层级：

| 工具 | 返回 |
| --- | --- |
| `get_run_status` | 运行状态、阶段、attempt、QC 状态、是否请求取消（不含工作区路径） |
| `list_run_artifacts` | 运行已登记产物的类型汇总与总数 |
| `get_statistical_results` | 该运行已登记的统计结果摘要 |
| `describe_statistical_capabilities` | 本安装实际支持的多重比较校正方法及其必需参数 |
| `list_skills` | 已评审 Skill 的标识、标题与状态 |
| `list_workspace_stages` | 当前选择工作区的服务端核验 DPABI 阶段候选；只返回摘要，不返回未授权文件内容 |
| `list_workspace_entries` / `search_workspace_content` / `read_workspace_content` | 在内容授权和用户点选范围内按需浏览、搜索、分段读取文件；结果视为不可信数据 |

工作区文件内容授权绑定会话工作区、Profile 配置修订和具体模型：

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| `GET` | `/conversations/{conversation_id}/workspace/consents` | 返回授权记录，并标出 `valid_for_current_context`；Profile 或工作区变化后旧授权不再有效 |
| `POST` | `/conversations/{conversation_id}/workspace/consents` | 用户确认外发后，向指定 Profile 和具体模型发送一次不含工作区信息的合成工具调用探针；仅探针成功时创建授权 |
| `DELETE` | `/conversations/{conversation_id}/workspace/consents/{consent_id}` | 撤销授权 |

Profile 必须声明 `tool_use`。探针不会执行工具；服务端要求响应包含指定的空参数 `tool_calls`。错误或超时返回 `workspace_tool_probe_failed`，配置未声明能力返回 `workspace_tool_use_required`。成功响应包含 `profile_revision` 与 `valid_for_current_context`。Profile 更新或删除重建、工作区变化、撤销以及切换到其他模型都会阻止工作区文件工具进入本轮目录。

统计设计的取值来源与装配规则见 [统计设计的取值来源与装配](../architecture/statistical-design-assembly.md)。

循环的最终文本就是 Work 原有的 JSON 契约，因此回复内容与 `action` 的解析、校验和授权路径与
单次路径完全一致。回复 payload 中的 `tool_loop` 记录 `enabled`、`stop_reason`、`steps`、
`tool_names` 与 `usage`，逐步审计记录与 Chat 相同（见下一节）。若循环产出的文本不是合法 JSON，
服务端会回退到单次调用重试一次，因此启用循环不会降低回答的结构保证。

Work 只绑定用户选择的工作区，目录中的 DPABI 输入由用户自行准备。方案请求可以只包含预处理参数并将 `requested_metrics`/`primary_outputs` 留空；此时不需要指标脑掩膜。通用 `/workspaces/check` 与 `/organization/previews` 仍供数据管理等独立功能使用，但不会由 Work 对话自动调用。

前端把 Model Profile 作为服务商 API 连接使用。绑定时调用 `/providers/models` 验证密钥并获取模型；
Agent 页面再次读取该连接的模型列表，并在每个对话回合用 `model` 字段提交用户实际选择的模型。
Profile 内部保存的 `model` 仅作为服务商暂时无法列出模型时的兼容回退，不要求用户额外维护。
设置页可编辑 Profile 能力。用户开启 `tool_use` 前需确认服务商支持工具调用；Work 文件授权随后会针对所选的具体模型执行独立探针。

Chat 与 Work 的回合请求均支持 `stream` 字段：`false`（默认）返回原有 JSON，
`true` 返回 HTTP 201、`Content-Type: text/event-stream`，客户端使用 POST fetch 读取响应体。
以下事件的 data 均为 JSON，帧以空行分隔：

| event | data | 含义 |
| --- | --- | --- |
| `status` | `{message: string}` | 当前响应准备状态 |
| `reset` | `{}` | 清空正文预览，隔离模型回退与工具轮次 |
| `delta` | `{text: string}` | Chat 追加正文增量；属于尚未校验的预览。Work 不发送未校验正文 |
| `final` | `ConversationTurnView` | 完整校验并落库的会话、消息、引用、草稿与工具步骤 |
| `error` | `{status: number, error: {code, message, details, trace_id}}` | 流已开始后的稳定错误封装；此时 HTTP 仍是 201 |

服务端每 15 秒发送 `: keep-alive` 注释，设置 `Cache-Control: no-cache, no-transform`
和 `X-Accel-Buffering: no`。不存在的会话和请求结构错误在流开始前按原 HTTP 错误返回。
客户端只有收到 final 才认定成功；若断流或收到不确定的错误，应保留原幂等键并显式重试。
断连后的已派发操作仍按原幂等协议收口，相同请求与键重放最终保存结果，不重复调用模型。

声明 `streaming` 能力且适配器支持流式的 Profile 使用 Provider 原生 SSE；其他配置返回完整正文，
仍通过相同流事件收口。内部意图、摘要和记忆 JSON 不展示。Work 在模型处理、工具检查和规划校验时发送固定中文 status，校验和保存完成后通过 final 展示正式回答。页面不显示未校验的 Work 正文增量，详见 [ADR 0023](../adr/0023-work-validated-answer-stream.md)。
Chat 的引用仍在完成后按本轮证据编号校验，前端以 final 的引用与正文为准。

Work 失败回复在 `assistant_message.payload.model_error` 中保存安全诊断：错误 code、处理 stage、
允许值的 finish_reason、token 用量、顶层校验字段及错误类型，已识别的 Provider 状态码或超时标记。
不保存模型原文、字段值、未知字段名或异常原文；本地日志只记录相同诊断。
结构不完整且 finish_reason 为 length 时提示提高输出上限；结构校验与参数草稿校验失败分别提示对应类别。
诊断不会修改参数草稿或赋予执行权限。历史失败轮次未记录这些诊断时，不能事后从通用提示推断具体原因。

ChatAgent 会把
执行型语言转换为 `work_request` draft 并保存到消息 payload，但不会创建计划、运行 MATLAB 或调用
Work Mode。普通回答中的 citation 只由类型化检索证据或 Provider 返回的 URL annotation 生成，
不会从模型自由文本中猜测来源。

Chat 回合支持可选 `paper_ids: string[]`；省略或空列表搜索全部可用文献，非空仅搜索所选已索引论文。
最近 12 条消息通过同一脱敏网关参与意图判断和回答；明确寒暄不检索，执行需求仍只生成草案。
正文 `[C1]` 编号对应本轮证据；无效编号移除并提示，`assistant_message.payload.chat.citations`
只保存实际引用来源。引用包含章节、摘录和物理页码，旧库缺失页码为 null。

## Literature 文献管理

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/literature/papers` | multipart 上传一篇 PDF，返回 Paper、sections、chunks 和解析 warnings |
| `GET` | `/literature/papers` | 列出已摄取论文及其结构化结果 |
| `GET` | `/literature/papers/{paper_id}` | 读取 Paper、sections 和可追溯 chunks |
| `POST` | `/literature/papers/{paper_id}/index` | 显式发送脱敏文本到 DashScope 并 upsert 索引，返回 PaperIngestResult |
| `GET` | `/literature/papers/{paper_id}/source` | 受控读取原 PDF，可配合 `#page=N` 定位物理页 |

PDF 原文件保存在 `allowed_work_root/literature/{paper_id}/source.pdf`，API/SQLite 只保存该规范相对路径。
默认大小上限 50 MiB。解析使用 `pypdf` 逐物理页抽取文本；无文本层、加密、损坏、伪 PDF 和超限文件
返回稳定错误，不会导致服务退出。Section detector 使用可扩展别名规则识别 Abstract、Introduction、
Methods 及 rs-fMRI 常见 Methods 子节、Results、Discussion、Conclusion、References；无法识别时保留为
Front Matter。Chunker 在 section 内按 paragraph/sentence 聚合，默认约 600 tokens、100 overlap，
每个 chunk 持久化 `paper_id`、section/subsection、物理页范围、token count、index 和 rs-fMRI metadata
占位。上传不自动建立索引。调用 index 后，同一 HTTP 请求完成索引，无后台队列。
Paper 增加 `index_status`（not_indexed/indexing/ready/failed）及可空 `index_error`；失败保留论文，
中断后可重新请求索引。迁移 `0009_literature_index` 将已有论文标为 not_indexed，不补建向量。
向量保存在 `allowed_work_root/literature_index` 的 `uploaded_literature_v1` collection，旧库保持不变。
索引需要可选 rag 依赖、DashScope Key 和既有脱敏配置；未通过外发校验的文本不会发送。
只有 ready 论文参与检索，两路候选只进行一次最终重排。

## 数据、人口学和划分

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/datasets/{dataset_id}` | 读取数据集 |
| `POST` | `/datasets/{dataset_id}/inspect` | 只读扫描并创建 manifest revision |
| `GET` | `/manifests/{manifest_id}` | 读取冻结 manifest |
| `POST` | `/datasets/{dataset_id}/demographics/import` | 从允许源根内的 CSV/TSV/XLSX 路径导入人口学信息 |
| `GET` | `/demographics/{demographics_id}` | 读取人口学 revision 摘要 |
| `POST` | `/datasets/{dataset_id}/splits` | 创建受试者级划分 revision |
| `GET` | `/splits/{split_id}` | 读取数据集划分 |

`source_roots`、`work_root`、数据集 `source_path` 和人口学 `source_path` 是少数允许客户端提交本机路径的字段。服务端会规范化路径并校验父子边界；执行计划不接受任意输出路径。

## Skill 和审批

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/skills` | 列出已登记 SkillSpec |
| `POST` | `/skill-plans/resolve` | 从用户意图和服务端谱系编译 SkillPlan |
| `GET` | `/plan-revisions/{plan_revision_id}` | 读取计划 revision |
| `POST` | `/plan-revisions/{plan_revision_id}/approve` | 批准或拒绝精确计划哈希 |

浏览器只提交 `SkillPlanIntent`：项目、数据集、manifest hash 和显式科研参数。输入 Artifact lineage、基础 Cfg Artifact 与环境锁由服务端派生，客户端不能伪造。

## 运行、事件和 Artifact

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/runs` | 从已批准且当前有效的计划创建 Mock 或逐次确认的 MATLAB 运行 |
| `GET` | `/runs` | 按项目/状态列出运行 |
| `GET` | `/runs/{run_id}` | 读取运行 |
| `POST` | `/runs/{run_id}/cancel` | 提交显式取消原因 |
| `POST` | `/runs/{run_id}/retry` | 在已批准重试预算内重试 |
| `POST` | `/runs/{run_id}/diagnosis` | 对受限日志片段执行本地确定性失败分类，不自动修复或重跑 |
| `GET` | `/runs/{run_id}/events` | SSE 事件；支持 `after_event_id`、`Last-Event-ID` 和 `once=true` |
| `GET` | `/runs/{run_id}/artifacts` | 列出运行的 Artifact 元数据 |
| `GET` | `/artifacts/{artifact_id}` | 读取单个 Artifact 元数据 |

`/runs` 默认创建 Mock 作业。显式选择 `execution_backend=matlab` 时，还必须满足本机执行开关、
环境锁、计划审批和逐次确认。`workspace_mode=in_place` 仅接受 DPABI-ready 数据集；Worker 把该数据集
目录传给 `DPARSFA_run` 作为 `WorkingDir`，DPABI stage/Results 留在所选工作区，脚本、日志、
证据和登记产物仍保存在隔离 attempt 目录。

## 扩展分析预览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/ml/datasets/inspect` | 在项目允许根内只读检查 CSV/TSV/XLSX |
| `POST` | `/ml/templates` | 生成待用户批准的固定 Python 机器学习模板 |
| `POST` | `/roi/extractions/validate` | 校验 ROI 提取参数与结构化长宽表合同 |
| `POST` | `/organization/previews` | 构建不修改源文件的 DPABI 整理复制预览 |
| `POST` | `/cluster-localizations` | 用用户提供的 atlas 坐标标签匹配 cluster 峰值 |
| `POST` | `/agent/rsfmri/questions` | 使用本地证据回答限定范围的 rs-fMRI 问题 |

这些接口不接受自由 MATLAB/Python/Shell 文本。真实 ROI 执行、文件复制、ML 训练和 NIfTI atlas 采样仍需后续的审批工作流与受控 Tool。联网搜索仅在 Chat 回合显式开启，并由具备相应能力的模型 Provider 执行。

## QC 和统计设计

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/qc-reviews` | 从类型化指标 Artifact 创建不可变 QC revision |
| `POST` | `/runs/{run_id}/head-motion-qc` | 复用已登记报告或从本次 attempt/原位工作区的 `RealignParameter` 重新解析并登记 `qc.head_motion_report`，再幂等生成头动 QC 草稿 |
| `POST` | `/runs/{run_id}/qc-skip` | 记录带 run version、操作者和原因的 QC 跳过决定；完成计算的运行进入 succeeded |
| `GET` | `/qc-reviews/{review_revision_id}` | 读取 QC revision |
| `POST` | `/qc-reviews/{review_revision_id}/approve` | 批准精确 QC hash 和冻结受试者顺序 |
| `POST` | `/statistical-designs` | 创建统计设计 revision |
| `GET` | `/statistical-designs/{plan_revision_id}` | 读取统计设计 |
| `POST` | `/statistical-designs/{plan_revision_id}/validate` | 校验设计并进入待审批 |
| `POST` | `/statistical-designs/{plan_revision_id}/approve` | 批准统计设计哈希 |
| `GET` | `/corrections` | 列出 FDR/GRF 校正能力和 schema |
| `POST` | `/statistics/runs` | 从已批准设计创建 Mock 或 MATLAB 统计运行 |
| `POST` | `/statistics/external-imports/inspect` | 在项目只读源根内检查分组 DPABI 指标目录，保存待审核记录 |
| `GET` | `/statistics/external-imports?project_id=...` | 列出状态、组别计数、网格及未知项摘要，不返回逐受试者路径 |
| `GET` | `/statistics/external-imports/{import_id}` | 读取完整检查、纳入决定和可选的统计输入引用 |
| `POST` | `/statistics/external-imports/{import_id}/include` | 以精确记录版本、纳入名单、组别、空间、未知项确认及可选 mask 冻结独立纳入决定 |
| `POST` | `/statistics/external-imports/{import_id}/demographics` | 以精确导入版本登记人口学表、编号列及字段对应，要求恰好覆盖纳入名单 |
| `GET` | `/statistics/external-imports/{import_id}/demographics` | 读取最新人口学修订的列名、人数、源表和内容哈希；不返回原始行 |
| `GET` | `/statistics/results?project_id=...&run_id=...` | 按项目（可选运行）列出已登记的统计结果摘要 |
| `GET` | `/statistics/results/{result_id}` | 读取冻结结果清单与 Markdown/JSON 复现报告 |
| `GET` | `/statistics/atlases` | 列出本机 DPABI 支持的 AAL、AAL3、Brodmann 图谱及可用性、分辨率和内容指纹 |
| `GET` | `/statistics/results/{result_id}/localization?atlas_id=aal` | 校验真实统计结果的簇范围与峰值，再用 DPABI 图谱仿射采样峰值标签和整簇脑区覆盖比例 |
| `GET` | `/statistics/results/{result_id}/roi?cluster_id=...` | 按冻结影像引用复核哈希/网格，返回所选簇逐受试者及条件的描述性指标均值；零结果返回空表 |
| `GET` | `/statistics/results/{result_id}/slice-info` | 返回统计图轴向层数、默认层及簇峰值所对应的原图层号 |
| `GET` | `/statistics/results/{result_id}/slice.png?slice_index=...` | 在结果权限和哈希校验后预览指定统计图轴向切片 |

结果明细增加可空 `method_summary`：检验、方向、协变量名称/中心化、自由度、冻结校正参数、实际 FDR p 截断及可核验的 GRF 实际平滑度/最小簇和软件版本。报告或设计绑定无效时摘要为空，历史原文仍可读。模型结果工具不转发完整报告；分析上下文与只读工具提供有界聚合事实，作用域绑定当前项目/运行。

`/statistics/runs` 的 `execution_backend` 显式选择 `mock` 或 `matlab`。Mock 运行使用醒目标记的非科学占位产物；MATLAB 固定模板覆盖三类 t 检验、相关／偏相关、多变量回归和单因素 ANOVA/ANCOVA 总体 F 检验，并支持 FDR/GRF 合同。每个真实统计结果登记设计矩阵、contrast、统计图、效应图、经完整性验证的簇表、校正证据、运行日志、软件证据与确定性复现报告。FDR 仅开放双侧 T/R 图并保存 q 与实际 p 截断值；GRF 保留参数及可用的 Z 图。六类统计已在本机 MATLAB R2024b、SPM25、DPABI V9.0_250415 上通过真实执行 smoke，能力目录会记录该本机验证状态。图谱定位复核簇表峰值数值和整簇 26 邻域范围；合成结果与空间未知的结果会被阻止定位。覆盖比例是描述性空间重叠，不是额外统计推断。

运行请求携带 `qc_mode=require|skip`。`require` 时，MATLAB/DPABI 预处理或指标运行成功并登记 Artifact 后，若本次计划启用了 Realign 且存在 `RealignParameter`，Worker 会解析 `HeadMotion.tsv`、FD 文件、`rp_*.txt` 和 DPABI 排除文件，登记 `qc.head_motion_report`，并自动创建 QC 草稿。含指标图的运行生成 `pre_statistics` scope，可在人工批准后作为统计来源；无指标图的纯预处理生成 `head_motion` scope。`skip` 时运行在产物登记完成后进入 `succeeded`，不解析头动文件或创建 QC 草稿。统计设计可绑定已批准 QC review，或绑定同一来源运行的 `qc_skip` 审计记录；后者会在报告中明确标记 QC 已跳过。默认阈值为最大平移/旋转 `≤2mm/2°`、mean FD Power `≤0.2`、FD Power > 0.5 比例 `≤20%`。

外部纳入请求的 `mask_relative_path` 必须指向导入根内的 NIfTI，并通过同网格和非空检查。没有 mask 仍可保存纳入记录，但详细响应 `selection` 为 null，不能建立可执行设计。有完整 mask 时，`selection` 返回服务端生成的影像引用、mask 引用和 `input_manifest_hash`。创建统计设计使用 `qc_source=external_import`、`external_import_id`、`external_import_hash`，其内部 QC review/skip 字段必须为空；服务端核对逐 subject 引用、分组及目录/mask 哈希。设计顺序可以显式打乱，不能改变 subject 与影像、组别的关系。外部人口学可独立登记修订并绑定协变量。inspect 的 layout 显式选择 grouped（默认）或 paired_conditions；配对布局必须恰好有两个完整条件目录，selection.images 增加 condition。纳入名单每 subject 一项，同时纳入两张图；配对 t 的 condition_order 明确引用两个标签。

## Provider 和 Agent

统计设计创建请求可携带 `atlas_id=aal|aal3|brodmann`；响应 `localization_plan` 返回服务端冻结的名称、内容哈希、空间、定位方法及连通性。缺省为 null，兼容历史设计。图谱不可用时返回 `atlas_unavailable`，冻结图谱改变时返回 `statistics_atlas_changed`（启动入口为包含该原因的 `plan_stale`）；定位请求不能替换方案图谱，违反时返回 `statistics_atlas_selection_mismatch`。零显著体素先返回 `no_significant_voxels`，不触发图谱和空间检查。

Work turn 可显式提交 `external_analysis_import_id`，与 `target_run_id` 互斥且不能指定预处理角色。服务端检查项目、纳入状态和 mask，向分析模型仅提供指标类型、`group` 列、组别、人数及人口学列名摘要。模型不能修改外部来源 ID/hash；方法完整时由服务端重新验证输入并形成统计设计预览。外部预览的 `source_run_id` 为 null，来源保存在 `design.external_import_id/hash`；审批与执行仍使用已有统计入口。外部协变量必须绑定 `external_demographics_revision_id/hash`；新人口学修订使旧方案失效。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/model-profiles` | 绑定或更新只引用本地密钥环境变量名的服务商连接（可携带 `api_key` 写入本地 `.env`） |
| `GET` | `/model-profiles` | 列出已绑定服务商 |
| `GET` | `/model-profiles/{profile_id}` | 读取服务商连接 |
| `DELETE` | `/model-profiles/{profile_id}` | 解除服务商连接 |
| `POST` | `/providers/models` | 列出某个 OpenAI 兼容 Provider 的可用模型（传 base_url + api_key 或密钥环境变量名） |
| `POST` | `/providers/test` | 发起一次脱敏的轻量连通性/schema smoke |
| `POST` | `/agent/tasks` | 执行结构化 Agent 任务 |
| `GET` | `/agent/tasks/{task_id}` | 读取 Agent 任务和路由决定 |

Agent 只返回结构化建议和未解决问题，不返回 Workflow transition 或可执行命令。外发上下文先经过失败关闭的脱敏策略；配置步骤见 [Provider 配置与 smoke](../operations/provider-setup.md)。
