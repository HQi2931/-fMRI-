# core

## 模块职责

`core` 定义框架最稳定的 Agent 抽象，包括 Agent 身份、请求响应、消息协议、任务对象、模型网关接口、推理策略接口和 AgentRuntime 边界。

## 模块边界

本模块不负责真实工具执行、持久化、检索、记忆写入、审批执行或领域业务。Agent 可以提出动作意图，但不能直接修改工作流状态或写入产物。

## 依赖关系

`core` 依赖通用接口和数据模型；运行时会调用 `context` 构建上下文，调用 `tools` 解析工具请求，调用 `workflow` 申请状态变更，并通过 `observability` 记录事件。

## 当前阶段

仅建立目录骨架和职责说明，未实现 Agent 循环、模型调用或策略逻辑。

## 后续核心接口

- `BaseAgent`
- `AgentRuntime`
- `ModelGateway`
- `Message`
- `Task`
- `ReasoningStrategy`
- `AgentRequest`
- `AgentResponse`

