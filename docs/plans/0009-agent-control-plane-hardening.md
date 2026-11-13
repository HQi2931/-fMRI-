# 计划 0009：Agent 控制面与执行授权加固

- 状态：Implemented
- 文档类型：Plan
- 维护者：system_architect
- 创建日期：2026-09-23
- 更新日期：2026-09-23
- 目标里程碑：Agent 边界、授权、预算与审计闭环
- 关联：[ADR 0014](../adr/0014-bounded-tool-loop.md)、[ADR 0015](../adr/0015-agent-control-plane-hardening.md)、[系统设计](../architecture/system-design.md)、[API 契约](../api/api-v1.md)、[数据外发策略](../security/data-egress.md)、[验证记录](../development/mvp-verification.md)

## 1. 背景与目标

当前 Agent 已具备工具分级、EXECUTE 目录隔离、结构化输出、外发脱敏、有限循环和会话审计。但现有实现仍有五个需要收口的控制面问题：

1. 工具声明了 `timeout_seconds`，但派发器没有真正执行单工具超时；循环墙钟也无法中止正在运行的 Provider 或工具调用。
2. Work 自然语言路径使用服务端签发的精确确认口令，显式 REST/UI action 则仍依赖客户端提交的 `real_execution_confirmed=true`，形成两套授权语义。
3. 工具调用已经发生、但循环最终回退到单次调用时，已完成的调用轨迹可能不进入会话审计。
4. Work 工具按全局 `run_id` 查询，没有把调用约束到当前 conversation/project。
5. 循环使用全局 token 上限，没有按照实际选中模型的上下文窗口为后续工具轮次预留空间。

本计划的目标不是扩大 Agent 权限，而是把现有安全承诺变成可执行、可测试的服务端不变量：

- 任一循环均受真实墙钟、单调用、步数和上下文预算约束。
- 任一真实执行只能消费服务端签发且单次有效的授权凭据。
- 任一已尝试的工具调用都具有持久化审计结果，包括回退和失败路径。
- 任一项目型工具只能读取当前对话已绑定项目内的对象。
- 改造保持当前 Chat/Work 用户体验与未声明 `tool_use` 模型的单次路径兼容。

## 2. 设计原则

1. **服务端状态是真源**：模型文本、UI 布尔值和请求文案都不是授权凭据。
2. **预算必须可强制执行**：配置值不能只是循环边界提示，超限必须产生确定的停止原因。
3. **先记录尝试，再解释结果**：审计不得依赖模型最终是否返回合法文本。
4. **最小作用域**：工具绑定 conversation、project 和允许访问的运行集合，不接受全局对象引用。
5. **失败关闭但不损害用户回复**：工具循环失败时仍可单次回退，但回退不能抹掉先前轨迹。
6. **不引入通用任务平台**：继续使用模块化单体、SQLite 和现有应用服务，不引入消息队列或分布式事务。

## 3. 目标控制流

```text
Conversation turn
  → build request-scoped AgentExecutionContext
  → select provider/profile and derive effective budget
  → run bounded loop
      → check remaining wall-clock/context budget
      → dispatch scoped READ/PROPOSE tool with per-call deadline
      → append immutable in-memory audit outcome
  → validate final response
      ├─ valid: use loop response
      └─ invalid/empty: single-shot fallback
  → atomically persist user message + assistant message + all loop outcomes

Real execution
  → server creates confirmation challenge
  → user confirms challenge through Chat/Work or UI
  → server atomically consumes challenge
  → approve exact frozen plan if permitted
  → create run
```

`AgentExecutionContext` 至少包含：

- `conversation_id`
- `project_id`
- `allowed_run_ids`
- `selected_profile_id`
- `context_window_tokens`
- `max_output_tokens`
- `deadline_monotonic`
- `cancellation_probe`
- `trace_id`

该对象由服务端构建，不接受模型或客户端直接赋值。

## 4. 工作包 A：真实预算与取消

### 4.1 有效预算

新增每轮动态预算计算：

```text
effective_input_budget = min(
  configured_agent_max_tool_tokens,
  floor((context_window_tokens - max_output_tokens) * safety_ratio)
)
```

初始建议 `safety_ratio=0.85`，与现有上下文引擎一致。每次 Provider 调用前按完整 transcript 重新估算；若剩余空间不足以容纳最小回答和一个工具结果，停止循环并返回 `budget_context`。

### 4.2 Provider deadline

- `ModelGateway.generate_tool_turn()` 接收本轮剩余时间。
- 实际 Provider timeout 取 `min(profile.timeout_seconds, remaining_seconds)`。
- Provider 调用结束后再次检查墙钟，超限不再派发其返回的工具调用。

### 4.3 工具 deadline

- `ToolDispatcher.dispatch()` 接收剩余总预算和取消探针。
- 单工具有效超时为 `min(tool.timeout_seconds, remaining_seconds)`。
- 异步 handler 通过 `asyncio.wait_for()` 强制超时。
- 同步 handler 不直接运行在事件循环线程；迁移到受控线程执行器。同步 handler 必须是只读或幂等操作，因为 Python 线程超时不能强制终止底层函数。
- 任何可能长时间占用本机进程的工具不得作为模型可见工具；真实 MATLAB 继续只走 Worker。

### 4.4 多工具响应

- 保留 Provider 层的单响应工具数上限，同时新增每轮累计调用上限。
- 每个 tool call 前重新检查取消、总墙钟和上下文余量。
- 达到预算后，尚未执行的调用记录为 `rejected/budget_exhausted`，不得静默丢弃。

### 4.5 模型变化

扩展 `AgentLoopStopReason`：

- `BUDGET_CONTEXT`
- `TOOL_TIMEOUT`
- `PROVIDER_TIMEOUT`

停止原因是审计事实，不直接作为用户科研结论。

## 5. 工作包 B：统一真实执行授权

### 5.1 ConfirmationChallenge

新增服务端持久化的一次性确认挑战，字段建议如下：

| 字段 | 语义 |
| --- | --- |
| `confirmation_id` | 不可预测标识 |
| `conversation_id` | 发起确认的对话 |
| `project_id` | 项目边界 |
| `action` | 目前仅允许受控执行动作 |
| `plan_revision_id` | 冻结计划 |
| `plan_hash` | 精确计划版本 |
| `qc_mode` | 用户审阅的 QC 选择 |
| `expires_at` | 短时有效期 |
| `consumed_at` | 单次消费记录 |
| `consumed_by` | `chat_exact_command` 或 `ui_confirmation` |
| `challenge_hash` | 展示内容与绑定字段的稳定哈希 |

数据库只保存挑战摘要和绑定字段，不保存密钥。挑战不可跨 conversation、project、plan 或 QC 选择复用。

### 5.2 双入口、单授权语义

- 自然语言入口继续展示人类可核对的精确命令，但命令内部对应一个服务端 challenge。
- UI 使用明确确认按钮，提交 `confirmation_id` 和挑战绑定信息；不再提交具有授权意义的布尔值。
- 旧 `real_execution_confirmed` 在迁移期仅作为兼容字段解析，但不能单独授权 MATLAB 运行。
- challenge 消费、计划批准和运行创建必须位于同一应用事务；任一步失败均不消费挑战。
- 重复提交返回已消费的确定结果或明确冲突，不创建第二个运行。

### 5.3 审计主体

- `ApprovalRecord.actor` 不再硬编码为 `work-conversation-agent`。
- 审计记录区分：用户交互通道、服务端执行组件和实际动作。
- 在尚无多用户身份系统时，使用 `local-user-via-work` 或 `local-user-via-ui` 等有限枚举，不伪造个人身份。

### 5.4 兼容策略

- 先增加 challenge API 和前端支持，再移除布尔授权。
- 默认关闭旧授权方式；如确需短期迁移，必须由本地配置显式开启，并记录弃用告警和审计事件。
- Worker 继续检查 `enable_real_execution`、环境探测、冻结审批和 payload 一致性，challenge 不替代纵深防御。

此工作包已经由 ADR 0015 固化。实现必须保持 challenge 绑定字段和单次消费语义，不得回退为独立布尔授权。

## 6. 工作包 C：回退路径审计完整性

### 6.1 统一 TurnPreparation

Chat 与 Work 的循环结果统一返回：

- `candidate_text`
- `stop_reason`
- `outcomes`
- `usage`
- `selected_profile`
- `attempted_profiles`
- `fallback_used`
- `fallback_reason`

即使 `candidate_text` 为空或不合法，`outcomes` 也必须保留到最终会话提交。

### 6.2 原子收口

- 工具执行阶段只收集不可变 `ToolCallOutcome`，当前 READ 工具不直接写会话审计表。
- 最终单次回退完成后，把用户消息、最终助手消息、循环 outcome、停止原因和 fallback 元数据放入同一事务。
- 若最终会话事务因乐观锁冲突失败，不应声称工具调用未发生；记录本地结构化诊断，并允许使用同一幂等键重放收口。

### 6.3 PROPOSE 工具前置条件

在以下条件全部满足前，不新增具有持久化副作用的 PROPOSE 工具：

1. handler 返回待提交 command，而不是立即写库；
2. command 与消息在同一事务提交；
3. 重试使用稳定幂等键；
4. 失败和回退测试证明不存在孤立草稿。

## 7. 工作包 D：项目与对象作用域

### 7.1 请求级 dispatcher

`dispatcher_factory` 改为接收 `AgentExecutionContext`。Work 工具不得直接绑定全局 `self.get_run`，而应绑定作用域包装器：

```text
get_scoped_run(context, run_id)
  → load run
  → require run.project_id == context.project_id
  → require run_id in context.allowed_run_ids or pass explicit project policy
  → return path-free projection
```

### 7.2 可见对象集合

初始范围建议只允许：

- 当前 `active_run_id`
- 当前计划直接关联的运行
- 用户在本轮明确选择、且服务端验证属于当前项目的运行

不允许模型通过全局枚举寻找其他项目对象。不存在项目绑定的 Work conversation 不提供项目型工具，只保留全局 Skill 与能力说明工具。

### 7.3 错误语义

跨项目、越过 allowlist 和对象不存在对模型统一返回 `not_available_in_scope`，避免通过差异错误探测对象是否存在；本地审计保留真实拒绝原因。

## 8. 工作包 E：上下文与工具结果治理

### 8.1 Transcript 预算

- 初始上下文、assistant tool-call 消息、工具结果和预留输出共享同一个 profile 预算。
- 工具结果首先执行现有脱敏和 byte 限制，再执行 token 限制。
- 不对结构化 ID、状态和错误码做截断；优先缩短说明文本、证据摘录和非关键列表。

### 8.2 上下文压缩

超过软阈值时，将较早工具结果转换为确定性摘要：工具名、状态、关键计数、引用 ID 和结果哈希。不得调用另一个模型生成安全摘要。

### 8.3 工具契约

每个模型可见工具新增并校验：

- `scope_kind`
- `idempotency`
- `side_effects`
- `timeout_seconds`
- `max_result_bytes`
- `max_result_tokens`

目录构建时拒绝缺失声明或与 tier 不一致的工具。

## 9. 分阶段实施顺序

### Phase 1：不变量与回归测试

1. 新增 ADR 0015，确定 confirmation challenge 和审计收口语义。
2. 为超时、回退审计、跨项目访问和小上下文模型先写失败测试。
3. 增加 `AgentExecutionContext` 与新的停止原因，不改变现有用户路径。

完成条件：新增测试能稳定复现五个问题，现有测试不被删除或放宽。

### Phase 2：预算执行与审计收口

1. 实现 Provider/工具 deadline。
2. 实现 profile 感知的 transcript 预算。
3. Chat/Work 回退路径持久化所有 outcome。

完成条件：人工注入卡住 handler、慢 Provider、超长工具结果和非法最终 JSON 时，请求在预算内结束且审计完整。

### Phase 3：作用域绑定

1. dispatcher 改为请求级构建。
2. Work 工具加入 project/run allowlist。
3. 前端和 API 对越界错误采用统一展示。

完成条件：两个项目拥有不同运行时，任一对话均不能通过工具读取另一项目的状态、产物或统计结果。

### Phase 4：统一确认挑战

1. 新增迁移、仓储和应用服务。
2. 接入 Work 精确命令与 UI 确认按钮。
3. 移除布尔值的独立授权能力。
4. 更新 API、OpenAPI、生成客户端和运维文档。

完成条件：自然语言与 UI 均消费同一种 challenge；过期、篡改、重放、跨项目和计划漂移全部失败关闭。

### Phase 5：验证与文档同步

1. 运行 Python、Web、文档和依赖门禁。
2. 使用 Mock Executor 完成执行授权 E2E。
3. 真实 MATLAB 只做经用户另行授权的小型 smoke，不作为本计划默认步骤。
4. 更新系统设计、API 契约、数据外发策略、验证记录和 CHANGELOG。

## 10. 测试矩阵

| 场景 | 预期结果 |
| --- | --- |
| 异步工具超过自身 timeout | `TOOL_TIMEOUT`，无后续派发，审计包含失败 outcome |
| Provider 超过剩余墙钟 | `PROVIDER_TIMEOUT`，请求在容差内返回 |
| 一个响应携带大量工具调用 | 只执行预算允许部分，其余明确拒绝 |
| 工具成功后模型返回非法 JSON | 单次回退成功，原工具 outcome 仍落库 |
| 会话版本在工具执行后发生变化 | 原子提交冲突，不产生半条会话；可幂等重试 |
| 项目 A 对话请求项目 B 的 run ID | `not_available_in_scope`，不返回 B 的存在性或状态 |
| 4k context profile 接收较大工具结果 | 结果被压缩或提前停止，不向 Provider 发送超窗请求 |
| challenge 被修改 QC 选择 | 拒绝，不批准、不入队、不消费 |
| challenge 重放 | 返回冲突或已有运行，不创建第二个运行 |
| challenge 过期或计划 hash 漂移 | 拒绝并要求重新审阅 |
| `enable_real_execution=false` | 即使 challenge 有效也不得启动 MATLAB |
| 模型猜测 EXECUTE 工具名 | 目录中不存在，保持当前拒绝行为 |

## 11. 验收标准

- `LlmTool.timeout_seconds` 有真实执行语义，并有异步、同步、总墙钟和取消测试。
- 任一循环对实际 profile 都不会构造超过其输入窗口的下一轮请求。
- 工具调用发生后，无论正常完成、回退、超时还是 Provider 故障，均能在最终会话中查询到有序 outcome。
- Work 项目型工具对跨项目 run/artifact/result 请求统一失败关闭。
- `real_execution_confirmed=true` 不能独立授权真实执行；只有未过期、未消费且绑定字段完全一致的 challenge 可以被消费。
- challenge 消费、必要的计划批准和运行创建原子完成，重复请求不产生重复运行。
- Agent 与 Worker 的纵深校验继续存在，未放宽冻结计划、环境锁、路径和 Artifact 谱系规则。
- Python 全量测试与覆盖率、Ruff、strict mypy、Web 测试与构建、文档校验全部通过。
- API/OpenAPI、系统设计、安全文档、验证记录和用户可见文案与实现一致。

## 12. 风险与缓解

| 风险 | 缓解措施 |
| --- | --- |
| 同步线程超时后底层函数仍运行 | 模型可见同步工具限定为只读/幂等；长任务不得进入 Agent dispatcher |
| challenge 增加交互复杂度 | 前端隐藏 token，只展示计划摘要、QC 选择、过期提示和一个明确确认动作 |
| 回退审计增加数据库体积 | outcome 保持现有脱敏与大小上限，工具结果保存摘要而非原始大对象 |
| 严格项目作用域影响旧对话 | 无项目绑定时禁用项目工具并提示先绑定项目，不自动猜测 |
| token 估算与 Provider 实际计数不同 | 保留安全余量，并记录估算值与 Provider usage 供校准 |
| 授权迁移期间存在两套路径 | 迁移期尽可能短；旧路径默认关闭并具备显式诊断事件 |

## 13. 非目标

- 不在本计划中增加多用户账户、RBAC 或远程部署能力。
- 不允许模型直接启动 MATLAB、批准计划或决定 QC。
- 不把 READ 工具扩展为任意文件读取、Shell、SQL 或自由 MATLAB 执行。
- 不改变 fMRI 科学参数、统计方法、DPABI 字段映射或现有数据输入边界。
- 不以本计划替代真实数据的方法学评审或人工 QC。

## 14. 交付物

1. ADR 0015：执行确认挑战与循环审计收口。
2. 请求级 `AgentExecutionContext` 和 profile 感知预算实现。
3. 可强制执行的 Provider/工具 deadline 与取消路径。
4. 跨项目失败关闭的 Work 工具目录。
5. `ConfirmationChallenge` 数据模型、迁移、API 和前端确认交互。
6. 回退仍保留 outcome 的统一 Chat/Work 持久化路径。
7. 对应单元、集成、API、前端与迁移测试。
8. 同步后的架构、接口、安全、运维和验证文档。
