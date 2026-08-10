# NeuroAgent Framework Architecture

状态：设计草案  
范围：通用科研 Agent 框架骨架与架构设计  
代码位置：[neuroagent](../../neuroagent/README.md)

本文档描述一个受 Hello-Agents 设计思想启发、但由本项目自主实现的通用科研 Agent 框架。本文不定义任何领域实现，不实现真实模型请求、真实 RAG、真实数据库、真实工具执行或生产级 Agent 循环。

## 1. 背景与目标

NeuroAgent 的目标是先建立一个通用科研 Agent 框架，再让具体科研领域以插件形式接入。静息态 fMRI 可以成为第一个领域插件，但不应成为框架核心。

不直接把 fMRI 功能写入 Agent 的原因：

- 领域流程、数据格式、外部软件、统计方法和质量控制规则变化快，若直接写入核心 Agent，会让框架难以复用。
- Agent 运行时需要处理权限、审计、状态恢复、上下文压缩和人工审批，这些能力与具体领域无关，应先抽象出来。
- 科研原始数据通常需要只读保护，领域插件必须通过受控执行和产物注册访问数据，不能让 Agent 任意修改。
- 框架核心如果混入领域概念，会让测试、权限白名单和错误恢复都变得不清晰。

先建立通用框架的原因：

- 统一 Agent、Task、Message、Tool、Workflow、Memory、Retrieval、Context、Artifact 和 Trace 的接口。
- 把 LLM 的不确定性决策与确定性执行分离。
- 让不同领域插件复用同一套审批、日志、产物、记忆隔离和上下文工程能力。
- 先用 Mock Model、Mock Tool 和 Mock Executor 完成可测试的最小闭环，再接入真实领域插件。

Hello-Agents 只作为设计参考，主要参考其 Agent 抽象、ReAct/Plan-Execute 思路、工具注册、记忆、检索、上下文工程、工作流、多 Agent、技能和可观测性理念。本项目不会直接把 Hello-Agents 作为业务运行时，也不会复制其存储结构或接口约定；NeuroAgent 将自主定义接口、运行时和存储结构。

## 2. 范围与非目标

本阶段范围：

- 框架架构。
- 核心抽象。
- 目录结构。
- Agent 运行流程设计。
- 核心数据模型草案。
- MVP 开发路线。
- 测试策略和完成标准。

本阶段非目标：

- DPABI。
- DPARSF。
- MATLAB 执行。
- fMRI 数据读取。
- 统计分析。
- 机器学习。
- 向量数据库部署。
- Web 前端。
- 完整生产代码。
- 真实 LLM 请求。
- 真实 RAG。
- 数据库迁移。

## 3. 设计原则

- 框架与领域解耦：核心包只包含通用概念，领域能力通过插件贡献工具、schema、工作流和技能。
- LLM 决策与确定性执行分离：模型只能生成动作意图，执行、状态变更、产物注册必须由确定性组件验证。
- 接口优先：先定义协议、数据模型和边界，再实现运行逻辑。
- 工具白名单：Agent 只能请求已注册、已授权、已声明 schema 和风险等级的工具。
- 最小权限：每个 Agent、Tool、Job 和 Plugin 只获得完成当前任务所需的最小权限。
- 人工审批：高风险工具、文件覆盖、跨边界访问、状态推进和敏感配置读取需要 ApprovalGate。
- 可追踪：上下文、检索、记忆、工具、审批、执行和产物都要产生日志事件。
- 可恢复：任务、工作流、Job 和产物应有可重试、可取消、可恢复的状态。
- 可替换：模型提供商、向量存储、持久化、对象存储、事件总线应通过接口替换。
- 可测试：所有核心流程都应支持 Mock LLM、Mock Tool、Mock Executor 和内存事件日志。
- 原始数据不可被 Agent 任意修改：原始科研数据默认只读，衍生产物必须注册为 ArtifactRef。
- Agent 不能直接修改工作流状态：Agent 只能请求 transition，由 WorkflowEngine 验证 guard、approval 和版本后执行。

## 4. 总体架构

NeuroAgent 采用单包模块化结构，当前根目录为 `neuroagent/`。依赖方向总体上从上层用例流向下层抽象，领域插件只通过公开扩展点接入。

| 层次 | 职责 | 依赖方向 |
| --- | --- | --- |
| Application Layer | 接收用户任务、展示状态、触发运行、读取产物和审计记录。 | 调用 Agent Runtime Layer，不直接操作工具和存储。 |
| Agent Runtime Layer | 加载 Agent、管理运行轮次、调用策略、协调上下文、工具、工作流和事件。 | 调用 Cognitive、Execution、Workflow、Observability 接口。 |
| Cognitive Layer | 提供推理策略、记忆、检索、上下文工程和技能加载。 | 调用 Infrastructure 抽象，不直接执行外部命令。 |
| Execution Layer | 管理工具运行时、JobExecutor、Sandbox、ArtifactManager 和审批后的执行。 | 调用 Infrastructure 的存储和事件接口。 |
| Infrastructure Layer | 提供 LLM、持久化、向量存储、对象存储、事件总线等适配接口。 | 不依赖上层业务规则。 |
| Domain Plugin Layer | 贡献领域工具、工作流、schema、技能和适配器。 | 依赖框架公开接口，不能反向修改核心。 |

建议的核心调用链：

```text
Application
→ AgentRuntime
→ ContextEngine
→ ReasoningStrategy / ModelGateway
→ ToolRegistry / PolicyEngine / ApprovalGate
→ JobExecutor / ArtifactManager
→ WorkflowEngine
→ EventBus / Trace / Audit
```

领域插件位于框架边缘。插件可以声明能力，但必须通过 ToolRegistry、PolicyEngine、WorkflowEngine 和 ApprovalGate 才能被调用。

## 5. 核心组件

### Model Gateway

解决问题：统一模型提供商调用方式，屏蔽不同 provider 的请求格式、流式输出、重试和错误模型。

调用关系：AgentRuntime 通过 ModelGateway 请求模型生成动作；ReasoningStrategy 决定提示结构和解析方式。

不属于本组件：上下文选择、工具执行、审批、工作流推进、领域知识解释。

建议接口：

- `generate(request: ModelRequest) -> ModelResponse`
- `stream(request: ModelRequest) -> Iterator[ModelDelta]`
- `count_tokens(payload: ModelPayload) -> TokenEstimate`

### Message Protocol

解决问题：统一系统消息、用户消息、Agent 消息、工具结果、审批结果和工作流事件的表达。

调用关系：AgentRuntime 读写 Message；ContextEngine 选择消息进入上下文；Trace/Audit 保存关键消息摘要。

不属于本组件：真实持久化、消息压缩策略、权限决策。

建议字段：`message_id`、`role`、`content`、`content_type`、`created_at`、`source`、`trace_id`、`metadata`。

### BaseAgent

解决问题：定义 Agent 身份、能力、默认策略、工具权限和上下文 profile。

调用关系：AgentRuntime 加载 BaseAgent；Multi-Agent Coordinator 通过 AgentIdentity 判断可交接对象。

不属于本组件：执行循环、工具实现、工作流状态持久化。

建议接口：

- `identity() -> AgentIdentity`
- `default_strategy() -> StrategyRef`
- `allowed_tools() -> list[ToolPermission]`
- `context_profile() -> ContextProfile`

### AgentRuntime

解决问题：管理一次 Agent run 的生命周期，把任务、上下文、模型动作、工具调用、审批、事件和最终响应串成可恢复流程。

调用关系：读取 Task，调用 ContextEngine，调用 ReasoningStrategy，校验 PolicyEngine，委托 Tool Runtime 或 ApprovalGate，最后请求 WorkflowEngine 更新状态。

不属于本组件：具体工具逻辑、具体检索后端、具体数据库实现、领域算法。

错误处理：任何策略解析失败、工具失败或审批拒绝都必须转为 RuntimeEvent，并由 WorkflowEngine 判断是否进入失败、等待审批或可重试状态。

### ReasoningStrategy

解决问题：把上下文和任务转换为模型可理解的推理流程，并把模型输出解析为结构化 AgentAction。

调用关系：AgentRuntime 调用策略；策略调用 ModelGateway；策略产出的动作交给 PolicyEngine 和 ToolRegistry。

不属于本组件：执行工具、保存产物、直接修改状态。

第一版规划 ReAct，后续支持 Direct、Plan-Execute、Reflection、Supervisor。

### ToolRegistry

解决问题：维护可用工具清单、输入输出 schema、风险等级、权限标签和插件来源。

调用关系：PolicyEngine 查询 ToolRegistry；AgentRuntime 根据模型动作定位工具；PluginLoader 向 ToolRegistry 注册贡献。

不属于本组件：真实执行、审批 UI、产物存储。

### Tool Runtime

解决问题：把合规 ToolCall 转换为受控执行请求，并把 JobResult 标准化为 ToolResult。

调用关系：接收 PolicyEngine 放行后的 ToolCall，委托 JobExecutor，返回 ToolResult 并记录事件。

不属于本组件：模型决策、工作流状态机、长期记忆。

### PolicyEngine

解决问题：集中处理工具权限、路径白名单、参数白名单、风险等级、速率限制、重试和熔断策略。

调用关系：AgentRuntime 在每次 ToolCall 或状态变更前调用 PolicyEngine；ApprovalGate 根据策略结果决定是否需要人工审批。

不属于本组件：人工审批交互、实际执行、产物写入。

### ApprovalGate

解决问题：对高风险动作建立结构化审批请求，记录审批人、理由、过期时间和结果。

调用关系：PolicyEngine 返回需要审批后，AgentRuntime 创建 ApprovalRequest；WorkflowEngine 可进入 waiting_approval 状态。

不属于本组件：自行放行工具、绕过状态机、修改原始数据。

### WorkflowEngine

解决问题：管理状态机、transition、guard、approval、artifact prerequisite、失败恢复和乐观锁。

调用关系：AgentRuntime 请求 transition；WorkflowEngine 验证当前 state、version、guard 和审批结果后提交 WorkflowEvent。

不属于本组件：模型调用、工具实现、知识检索。

### MemoryService

解决问题：管理不同类型记忆的写入、召回、验证、更新、supersede、遗忘和 scope 隔离。

调用关系：ContextEngine 召回记忆；AgentRuntime 或 WorkflowEngine 只能通过受控接口写入候选记忆。

不属于本组件：把未验证内容当事实、跨项目共享记忆、直接替代检索证据。

### RetrievalService

解决问题：把知识文档转换成可引用证据，支持关键词搜索、向量搜索、融合和重排。

调用关系：ContextEngine 根据任务请求 EvidenceChunk；RetrievalService 返回证据和 provenance。

不属于本组件：替 Agent 决策、部署具体向量数据库、执行领域分析。

### ContextEngine

解决问题：按 Gather、Select、Structure、Compress 流程构建 ContextPacket，并保存 ContextSnapshot 便于复现。

调用关系：从 Task、Message、Workflow、Memory、Retrieval、ArtifactRef 收集材料；输出给 ReasoningStrategy。

不属于本组件：模型调用、工具执行、记忆验证。

### SkillManager

解决问题：加载任务相关技能，包括专业知识、行为规范、提示片段、检查清单和约束说明。

调用关系：ContextEngine 请求 SkillManager 提供可注入上下文；PluginLoader 可贡献 SkillSpec。

不属于本组件：注册工具、执行代码、修改工作流。

### JobExecutor

解决问题：执行受控 Job，提供状态、超时、取消、重试和日志采集。

调用关系：Tool Runtime 创建 JobSpec；JobExecutor 返回 JobResult；ArtifactManager 注册输出。

不属于本组件：任意 Shell 入口、模型推理、审批决策。

### ArtifactManager

解决问题：注册输出文件、校验 checksum、保存 provenance，并用 ArtifactRef 表示大文件。

调用关系：JobExecutor 完成后交给 ArtifactManager；ContextEngine 只读取摘要和引用，不直接装载大文件。

不属于本组件：生成业务结果、解释领域含义、覆盖未批准文件。

### Multi-Agent Coordinator

解决问题：支持 Supervisor 和 Sequential Handoff，把任务拆分给不同 Agent，并保持权限和上下文隔离。

调用关系：Supervisor 通过 AgentRegistry 选择子 Agent，通过 AgentHandoff 交接结构化摘要。

不属于本组件：共享完整聊天记录、绕过父级工作流、共享未授权记忆。

### EventBus

解决问题：传递 RuntimeEvent，使运行过程可追踪、可审计、可恢复。

调用关系：所有核心模块发布事件；Trace/Audit 订阅并持久化关键事件。

不属于本组件：业务决策、事件内容解释、长期存储选型。

### Trace/Audit

解决问题：记录 trace_id 下的上下文构建、记忆召回、证据检索、工具调用、审批、状态变更、产物和失败原因。

调用关系：AgentRuntime 创建 TraceContext；各模块写 RuntimeEvent；Audit 记录不可抵赖的关键动作。

不属于本组件：执行动作、放行权限、修改状态。

## 6. 核心数据模型

本阶段只定义建议字段和关系，不实现数据库。字段命名应稳定，存储方式待后续决策。

### 标识关系

```text
project_id
└── session_id
    └── task_id
        └── run_id
            ├── agent_id
            └── trace_id
```

- `project_id`：项目级隔离边界。记忆、检索 namespace、产物和权限默认不能跨项目共享。
- `session_id`：一次用户交互或协作会话，可包含多个任务。
- `task_id`：一个可追踪目标，通常绑定一个 WorkflowInstance。
- `run_id`：某个任务的一次执行尝试；重试或恢复应生成新 run 或保留 attempt 字段。
- `agent_id`：执行当前 run 或 handoff 的 Agent 身份。
- `trace_id`：贯穿上下文、模型、工具、审批、工作流和产物事件的审计链路；子 Agent 可有 child trace。

### 数据模型草案

| 模型 | 建议字段 | 关系与说明 |
| --- | --- | --- |
| `AgentIdentity` | `agent_id`、`name`、`role`、`version`、`capabilities`、`allowed_tools`、`context_profile`、`policy_profile` | 描述 Agent 能力和权限，不包含具体运行状态。 |
| `AgentRequest` | `request_id`、`project_id`、`session_id`、`task_id`、`run_id`、`agent_id`、`messages`、`context_packet_id`、`requested_at` | AgentRuntime 的输入边界。 |
| `AgentResponse` | `response_id`、`run_id`、`agent_id`、`status`、`final_message`、`actions`、`artifact_refs`、`trace_id` | 返回用户或上层应用的结构化结果。 |
| `Message` | `message_id`、`session_id`、`task_id`、`role`、`content`、`content_type`、`created_at`、`source`、`trace_id` | 统一人、Agent、工具和系统消息。 |
| `Task` | `task_id`、`project_id`、`session_id`、`title`、`description`、`status`、`priority`、`workflow_instance_id`、`created_at` | 用户目标与工作流实例的连接点。 |
| `AgentAction` | `action_id`、`run_id`、`type`、`thought_summary`、`tool_call`、`handoff`、`final_response`、`created_at` | 模型输出后的结构化动作，不能直接执行。 |
| `ToolCall` | `tool_call_id`、`tool_id`、`input`、`risk_level`、`requested_by`、`timeout_seconds`、`trace_id` | 需要 ToolRegistry 和 PolicyEngine 校验。 |
| `ToolResult` | `tool_result_id`、`tool_call_id`、`status`、`output`、`error`、`job_id`、`artifact_refs`、`completed_at` | 工具执行结果，只能由受控运行时产生。 |
| `WorkflowDefinition` | `workflow_definition_id`、`name`、`version`、`states`、`transitions`、`guards`、`approval_rules` | 工作流模板。 |
| `WorkflowInstance` | `workflow_instance_id`、`definition_id`、`task_id`、`state`、`version`、`started_at`、`updated_at` | 具体任务的状态机实例。 |
| `WorkflowEvent` | `workflow_event_id`、`workflow_instance_id`、`from_state`、`to_state`、`transition`、`actor`、`reason`、`trace_id` | 状态变化审计记录。 |
| `ApprovalRequest` | `approval_request_id`、`project_id`、`task_id`、`run_id`、`risk_level`、`requested_action`、`status`、`reviewer`、`expires_at` | 高风险动作的人审入口。 |
| `MemoryRecord` | `memory_id`、`scope`、`type`、`content`、`source`、`confidence`、`verification_status`、`supersedes`、`created_at` | 记忆单元，必须有来源和验证状态。 |
| `MemoryScope` | `scope_id`、`project_id`、`session_id`、`agent_id`、`visibility`、`ttl` | 控制记忆隔离和遗忘策略。 |
| `KnowledgeDocument` | `document_id`、`namespace`、`title`、`source_uri`、`checksum`、`metadata`、`trust_level`、`ingested_at` | 检索文档的来源记录。 |
| `EvidenceChunk` | `chunk_id`、`document_id`、`namespace`、`text`、`span`、`score`、`metadata`、`citation`、`provenance` | RAG 返回的证据片段，不等于已确认事实。 |
| `ContextPacket` | `context_packet_id`、`run_id`、`sections`、`token_budget`、`source_refs`、`created_at` | 传给策略和模型的当前上下文包。 |
| `ContextSnapshot` | `context_snapshot_id`、`context_packet_id`、`trace_id`、`hash`、`redactions`、`created_at` | 用于复现和审计的上下文快照。 |
| `JobSpec` | `job_id`、`tool_call_id`、`executor_type`、`input`、`working_dir_policy`、`timeout_seconds`、`cancel_token` | 受控执行任务定义。 |
| `JobResult` | `job_id`、`status`、`stdout_ref`、`stderr_ref`、`exit_code`、`error`、`artifact_refs`、`completed_at` | 执行结果，输出大内容应通过引用保存。 |
| `ArtifactRef` | `artifact_id`、`project_id`、`task_id`、`run_id`、`path_or_uri`、`mime_type`、`size_bytes`、`checksum`、`provenance` | 产物引用，大文件只通过该模型进入上下文。 |
| `AgentHandoff` | `handoff_id`、`parent_run_id`、`child_agent_id`、`summary`、`task_contract`、`allowed_context_refs`、`permissions` | 多 Agent 交接结构，禁止直接传完整聊天记录。 |
| `RuntimeEvent` | `event_id`、`trace_id`、`run_id`、`event_type`、`payload`、`severity`、`created_at` | 所有关键动作的统一事件格式。 |

## 7. Agent 运行循环

标准运行循环：

```text
创建任务
→ 加载 Agent
→ 读取工作流状态
→ 构建上下文
→ 模型生成动作
→ 权限和策略校验
→ 工具执行或人工审批
→ 保存事件与产物
→ 更新上下文
→ 继续下一轮
→ 完成或失败
```

详细流程：

1. Application 创建 Task，并绑定 WorkflowInstance。
2. AgentRuntime 根据 AgentIdentity 加载 Agent 配置、默认策略和权限 profile。
3. AgentRuntime 读取 WorkflowInstance 当前状态和版本。
4. ContextEngine 收集任务、消息、工作流状态、记忆、证据和产物引用，生成 ContextPacket 与 ContextSnapshot。
5. ReasoningStrategy 调用 ModelGateway，解析模型输出为 AgentAction。
6. 如果 AgentAction 是 ToolCall，ToolRegistry 验证工具存在，PolicyEngine 验证权限、参数、路径、风险和速率。
7. 低风险动作可由 Tool Runtime 委托 JobExecutor 执行；高风险动作进入 ApprovalGate。
8. JobExecutor 返回 JobResult，ArtifactManager 注册产物，Observability 记录 ToolCompleted、ArtifactCreated 或失败事件。
9. AgentRuntime 只能向 WorkflowEngine 请求状态变更。WorkflowEngine 验证 transition、guard、approval、artifact prerequisite 和状态版本后提交 WorkflowEvent。
10. ContextEngine 在下一轮读取更新后的状态和事件。
11. 达到完成条件时生成 AgentResponse；失败时记录 AgentFailed 并进入可恢复或终止状态。

关键约束：Agent 只能“请求”工作流状态变更，不能直接写状态字段。所有状态变化必须通过 WorkflowEngine。

## 8. 推理策略

统一接口建议：

```text
ReasoningStrategy.prepare(context_packet) -> ModelRequest
ReasoningStrategy.parse(model_response) -> AgentAction
ReasoningStrategy.observe(tool_result | workflow_event) -> StrategyState
ReasoningStrategy.should_continue(strategy_state) -> bool
```

策略类型规划：

- Direct：一次模型调用直接生成最终响应，适合低风险问答和摘要。
- ReAct：模型按 Thought/Action/Observation 的结构迭代，适合工具辅助任务。MVP 0.1 优先规划该策略。
- Plan-Execute：先生成计划，再逐步执行，适合长任务。
- Reflection：执行后自检和修正，适合需要质量门槛的任务。
- Supervisor：父 Agent 拆分任务并协调子 Agent，适合多角色协作。

第一版 ReAct 约束：

- Thought 不要求持久化完整推理链，只保存可审计的 `thought_summary`。
- Action 必须是结构化 `AgentAction`，不能是自由文本命令。
- Observation 来自 ToolResult、WorkflowEvent、ApprovalResult 或 RuntimeEvent。
- 策略不执行工具，不修改状态，只产出下一步意图。

## 9. 工具与权限系统

工具定义必须包含：

- 工具 ID、名称、版本、描述和插件来源。
- 输入 schema 和输出 schema。
- 风险等级：`read_only`、`write_limited`、`destructive`、`external_access` 等。
- Agent 权限：哪些 Agent 可见、可调用、可传入哪些参数。
- 路径白名单：允许读取、写入或生成产物的位置。
- 参数白名单：枚举值、路径模式、最大文件大小、最大输出长度等。
- 超时、重试、熔断和并发限制。
- 是否需要人工审批。
- 审计字段：调用者、输入摘要、输出摘要、产物引用、错误信息和 trace_id。

调用流程：

```text
AgentAction
→ ToolRegistry lookup
→ schema validation
→ PolicyEngine validation
→ ApprovalGate if needed
→ Tool Runtime
→ JobExecutor
→ ToolResult
→ RuntimeEvent
```

错误处理：

- schema 校验失败：返回 ToolResult `invalid_input`，不执行。
- 权限失败：返回 `permission_denied`，记录 ToolRequested 和拒绝原因。
- 审批拒绝：进入 `approval_rejected`，由 WorkflowEngine 判断后续状态。
- 超时或熔断：返回可重试或不可重试错误，并记录 retry budget。

## 10. 工作流系统

工作流用于把科研任务拆成可验证状态，而不是让 Agent 自由推进。

核心概念：

- 状态机：每个 WorkflowDefinition 声明允许状态。
- transition：状态之间的合法迁移。
- guard：迁移前必须满足的条件。
- approval：某些迁移或工具结果需要人工审批。
- artifact prerequisite：某些状态必须存在指定 ArtifactRef 才能进入。
- 失败状态：区分可重试失败、等待输入、审批拒绝和终止失败。
- 恢复策略：从最近可恢复状态重新运行，或创建新 run。
- 状态版本：WorkflowInstance 使用乐观锁，避免并发覆盖。

建议状态：

```text
created
running
waiting_approval
waiting_input
blocked
completed
failed_retryable
failed_terminal
cancelled
```

Agent 运行时不能直接设置这些状态。它只能提交：

```text
TransitionRequest {
  workflow_instance_id,
  expected_version,
  requested_transition,
  reason,
  evidence_refs,
  artifact_refs,
  trace_id
}
```

WorkflowEngine 验证后产出 WorkflowEvent。

## 11. 记忆系统

记忆分为四类：

- Working Memory：当前任务短期信息，生命周期通常绑定 run 或 task。
- Episodic Memory：历史事件和经验，保留来源、时间和上下文。
- Semantic Memory：经过验证的稳定知识，必须有来源、置信度和验证状态。
- Procedural Knowledge：操作规范、检查清单和流程性知识，可由 Skill 或 Plugin 贡献。

记忆规则：

- Scope 隔离：默认按 `project_id` 隔离，必要时再按 `session_id`、`task_id`、`agent_id` 收窄。
- 来源：每条 MemoryRecord 必须记录 source，可以是用户确认、工具结果、文档证据或人工录入。
- 置信度：区分候选、低置信、中置信、高置信。
- 验证状态：`unverified`、`verified`、`rejected`、`superseded`。
- 版本更新：新记忆不能覆盖旧记忆，只能通过 `supersedes` 标记替代关系。
- 遗忘：支持 ttl、用户请求删除、项目归档和策略性压缩。
- 项目隔离：不同项目之间禁止串记忆，除非用户明确建立共享知识库并通过权限审批。

MemoryService 不应把检索结果自动写入 Semantic Memory。检索证据需要经过验证或人工确认后才能升级。

## 12. 检索系统

RetrievalService 的目标是返回证据，不是替 Agent 做决策。

流程：

```text
KnowledgeDocument
→ ingestion
→ parsing
→ chunking
→ metadata enrichment
→ embedding / keyword index
→ namespace storage
→ query
→ keyword search + vector search
→ fusion
→ reranking
→ EvidenceChunk
```

关键设计：

- 文档摄取：记录 source_uri、checksum、ingested_at 和 trust_level。
- 解析：提取文本、表格、标题、页码、段落或其他可引用位置。
- 分块：保留 chunk_id、document_id、span、metadata 和 citation。
- metadata：项目、命名空间、文档类型、作者、日期、权限范围。
- embedding：通过抽象接口接入，当前不部署具体向量数据库。
- keyword search：保留可解释召回能力。
- vector search：用于语义召回，但不能替代引用。
- fusion：合并关键词与向量结果。
- reranking：提升相关性，但保留原始分数和来源。
- EvidenceChunk：进入上下文时必须带 citation/provenance。
- namespace：默认按 project_id 或知识库隔离。
- trust level：区分用户提供、项目文档、外部文献、临时结果等来源。

ContextEngine 可以引用 EvidenceChunk，但 Agent 仍需说明证据如何支持结论。检索结果不能直接写成已确认事实。

## 13. 上下文系统

ContextEngine 参考 Gather-Select-Structure-Compress 思路。

组件：

- Gatherer：从 Task、Message、WorkflowInstance、MemoryRecord、EvidenceChunk、ArtifactRef 和 SkillSpec 收集候选材料。
- Selector：按任务目标、Agent 角色、权限、时间、置信度和 token budget 选择材料。
- Structurer：把材料组织为系统约束、任务目标、状态摘要、可用工具、证据、记忆、产物引用和输出要求。
- Compressor：压缩历史消息、日志、表格、代码和长文档，保留引用和可追溯来源。
- TokenBudget：为系统约束、任务描述、证据、记忆、历史消息和输出空间分配预算。
- ContextSnapshot：保存构建结果的 hash、来源列表、删减记录和 trace_id。

上下文隔离：

- 不同 Agent 使用不同 context profile。
- 子 Agent 只能接收 AgentHandoff 中允许的摘要和引用。
- 敏感配置、未授权文件内容、原始大文件和跨项目记忆不得进入模型上下文。

压缩规则：

- 日志：优先保留错误、状态变化、审批和工具结果摘要。
- 表格：保留列名、行数、关键统计和必要样例，不直接展开大表。
- 代码：保留相关函数、接口和调用关系，避免整仓库注入。
- 历史消息：保留用户目标、已确认决策、未解决问题和关键约束。

## 14. Skills 与插件系统

Skill 和 Plugin 必须区分：

- Skill：可加载的专业知识、行为规范、提示模板、检查清单和工作习惯。它影响上下文和 Agent 行为，但不直接提供执行能力。
- Plugin：包含工具、工作流、schema、技能和领域实现的扩展包。它通过公开接口向框架注册能力。

插件接入流程：

```text
PluginLoader
→ read PluginSpec
→ register skills
→ register tools
→ register workflow definitions
→ register schemas
→ register policies
→ expose capabilities to AgentRuntime
```

未来 `neuroagent-fmri` 可作为独立插件接入：

- 提供领域 Skill，描述数据处理规范、质量控制要求和报告风格。
- 提供领域 Tool，但必须声明 schema、风险等级、路径白名单和审批要求。
- 提供领域 WorkflowDefinition，描述领域任务状态机。
- 提供领域 Artifact schema，描述产物类型和 provenance。
- 不修改 `neuroagent/core`、`neuroagent/workflow`、`neuroagent/tools` 等核心模块。

本阶段不创建该插件的具体实现。

## 15. 执行与 Artifact 系统

Agent 不直接调用任意 Shell。所有执行必须经过：

```text
ToolCall
→ PolicyEngine
→ ApprovalGate if needed
→ JobSpec
→ JobExecutor
→ JobResult
→ ArtifactManager
→ ArtifactRef
```

Job 状态建议：

```text
queued
running
cancelling
cancelled
succeeded
failed_retryable
failed_terminal
timed_out
```

执行规则：

- JobSpec 声明 executor_type、输入、工作目录策略、超时、取消 token 和输出声明。
- JobExecutor 不接受自由文本命令作为默认入口；具体执行器必须受 Tool schema 约束。
- 超时后应尝试取消并保存部分日志引用。
- 输出文件必须由 ArtifactManager 注册，记录 checksum、size、mime_type、producer、source_job_id 和 trace_id。
- 大文件只通过 ArtifactRef 进入上下文，ContextEngine 最多读取摘要、预览或元数据。
- 未经批准不得覆盖已有文件；写入路径必须在白名单内。

## 16. 多 Agent 协作

第一版只支持：

```text
Supervisor
Sequential Handoff
```

Supervisor 负责：

- 判断是否需要子 Agent。
- 选择具备合适 capability 的 Agent。
- 生成结构化 AgentHandoff。
- 收集子 Agent 结果并请求 WorkflowEngine 推进状态。

AgentHandoff 必须包含：

- 交接目标。
- 任务边界。
- 允许访问的 ContextSnapshot 或 ArtifactRef。
- 子 Agent 工具权限。
- 输出契约。
- 失败回传方式。

禁止做法：

- 直接把完整聊天记录传给子 Agent。
- 共享父 Agent 的全部记忆。
- 给子 Agent 继承父 Agent 的全部工具权限。
- 让子 Agent 直接推进父工作流状态。

## 17. 可观测性和审计

至少记录以下 RuntimeEvent：

- `ContextBuilt`
- `MemoryRecalled`
- `EvidenceRetrieved`
- `ToolRequested`
- `ToolCompleted`
- `ApprovalRequested`
- `WorkflowTransitioned`
- `ArtifactCreated`
- `AgentCompleted`
- `AgentFailed`

事件字段建议：

- `event_id`
- `event_type`
- `project_id`
- `session_id`
- `task_id`
- `run_id`
- `agent_id`
- `trace_id`
- `severity`
- `payload`
- `created_at`

审计要求：

- 高风险动作必须记录审批请求和审批结果。
- 文件写入、覆盖、删除和外部访问必须记录路径或 URI 摘要。
- 上下文快照应记录来源和删减，不应无控制保存敏感原文。
- 失败事件应包含可恢复建议，例如 retry、wait_input、wait_approval 或 terminal。

## 18. 安全边界

安全边界必须作为框架能力内建，而不是依赖 Agent 自觉遵守。

- 禁止任意 Shell：Agent 只能调用已注册工具，工具再转换为受控 Job。
- 禁止未经批准覆盖文件：覆盖、删除或跨边界写入必须需要审批。
- 禁止跨项目数据访问：`project_id` 是默认隔离边界。
- 禁止 Agent 自行修改已验证记忆：只能提交 MemoryUpdateRequest，由策略验证。
- 禁止检索结果直接作为已确认事实：EvidenceChunk 需要引用和验证。
- 禁止工具绕过 WorkflowEngine：工具结果只能请求状态变更，不能直接写状态。
- 敏感配置不得进入模型上下文：密钥、token、连接串和私密配置只在受控执行层使用。
- 原始科研数据默认只读：衍生产物和中间结果写入单独输出区域，并注册 ArtifactRef。

## 19. MVP 开发路线

### MVP 0.1

目标：完成最小 Agent 运行骨架，使用 Mock 组件验证一轮 ReAct 风格动作。

- Model Provider。
- Message。
- BaseAgent。
- ReActStrategy。
- BaseTool。
- ToolRegistry。
- ToolResult。
- AgentRuntime。
- Event log。
- 简单测试工具。

完成标准：

- 可用 Mock Model 生成结构化 AgentAction。
- 可用 Mock Tool 返回 ToolResult。
- 可记录 ContextBuilt、ToolRequested、ToolCompleted、AgentCompleted 或 AgentFailed。
- 不依赖真实外部服务。

### MVP 0.2

目标：加入工作流、权限和产物边界。

- WorkflowEngine。
- PolicyEngine。
- ApprovalGate。
- TaskStore。
- ArtifactManager。

完成标准：

- Agent 不能直接改状态。
- 高风险动作可进入 waiting_approval。
- ArtifactRef 能记录 checksum 和 provenance。
- 状态机有单元测试和失败路径测试。

### MVP 0.3

目标：加入记忆、检索和上下文工程。

- Working/Episodic/Semantic Memory。
- RetrievalService。
- ContextEngine。
- ContextSnapshot。

完成标准：

- 记忆按 project_id 隔离。
- RetrievalService 返回 EvidenceChunk 和 provenance。
- ContextSnapshot 可复现一次模型输入。
- token budget 有测试覆盖。

### MVP 0.4

目标：加入多 Agent 协作。

- AgentRegistry。
- Supervisor。
- Handoff。
- 子 Agent 权限隔离。

完成标准：

- Supervisor 可生成结构化 AgentHandoff。
- 子 Agent 只接收允许的上下文引用。
- 子 Agent 工具权限不继承父 Agent 全量权限。
- Handoff 流程有集成测试。

达到 MVP 0.4 且通过测试后，才建议开始第一个领域插件开发。

## 20. 测试策略和完成标准

测试层次：

- 单元测试：数据模型、schema 校验、策略解析、权限判断、状态机 transition。
- 集成测试：AgentRuntime + Mock Model + Mock Tool + WorkflowEngine + Event log。
- 端到端测试：从 Task 创建到 AgentResponse、ArtifactRef 和 RuntimeEvent 完整闭环。
- Mock LLM：固定输出，覆盖正常动作、无效动作、格式错误和拒绝执行。
- Mock Tool：覆盖成功、失败、超时、权限拒绝和产物生成。
- Mock Executor：覆盖 Job 状态、取消、重试和日志引用。
- 状态机测试：非法 transition、版本冲突、guard 失败和恢复流程。
- 权限测试：工具白名单、路径白名单、参数白名单和审批触发。
- 记忆隔离测试：不同 project_id、session_id 和 agent_id 不串记忆。
- 上下文 token 预算测试：超预算压缩、敏感信息删减和引用保留。
- Agent handoff 测试：只传结构化摘要和授权引用。

开始开发第一个领域插件的前置条件：

- 核心目录和接口稳定。
- MVP 0.1 至 0.4 的核心测试通过。
- ToolRegistry、PolicyEngine、WorkflowEngine、ArtifactManager 和 Trace/Audit 已有最小实现。
- 原始数据只读、产物注册、审批和恢复路径经过测试。
- 插件注册机制能在不修改核心包的情况下加载工具、技能和工作流定义。

## 21. 待确认问题

以下问题需要后续决策，不应在当前信息不足时锁定生产选型：

- Python 版本。
- Pydantic 版本。
- 是否采用 async-first 模型。
- PostgreSQL 或 SQLite。
- pgvector 或 Qdrant。
- 本地文件存储或对象存储。
- EventBus 选型。
- 长期采用单包还是 monorepo。
- 是否使用现有 Web 框架。
- Hello-Agents 参考到什么程度：概念参考、接口参考或仅架构启发。
- 框架最终命名是否固定为 NeuroAgent。
- 插件包命名规范和发布方式。
- 审批记录的持久化与合规要求。
- 上下文快照是否保存原文、摘要或加密引用。
- 领域插件的测试数据如何隔离和脱敏。

