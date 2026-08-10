# multi_agent

## 模块职责

`multi_agent` 负责 Agent 注册、Supervisor 协调、Sequential Handoff 和结构化交接信息，支持多 Agent 在权限隔离下协作。

## 模块边界

本模块不把完整聊天记录直接传给子 Agent，不共享未授权记忆，不绕过父级策略，也不替代工作流状态机。

## 依赖关系

`multi_agent` 依赖 `core` 的 Agent 抽象、`context` 的上下文隔离能力、`workflow` 的状态约束和 `observability` 的 Trace/Audit。

## 当前阶段

仅建立 registry、coordinator 和 handoff 的目录边界，未实现 Supervisor 或 Handoff。

## 后续核心接口

- `AgentRegistry`
- `Supervisor`
- `AgentHandoff`
- `HandoffPolicy`
- `AgentCapability`

