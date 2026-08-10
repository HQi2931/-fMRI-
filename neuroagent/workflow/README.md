# workflow

## 模块职责

`workflow` 负责工作流定义、状态机、transition、guard、approval、artifact prerequisite、失败状态、恢复策略和状态版本控制。

## 模块边界

Agent 不能直接修改工作流状态，只能提交状态变更请求。`workflow` 不负责模型推理、工具执行细节、文档检索或记忆排序。

## 依赖关系

`workflow` 由 `core` 的运行时调用；它会使用 `tools` 和 `execution` 的结果判断状态变更，使用 `observability` 记录 WorkflowTransitioned、ApprovalRequested 等事件。

## 当前阶段

仅建立 definitions、engine、state 和 approvals 的目录边界，未实现状态机。

## 后续核心接口

- `WorkflowDefinition`
- `WorkflowInstance`
- `WorkflowEvent`
- `WorkflowEngine`
- `TransitionGuard`
- `ApprovalGate`
- `ApprovalRequest`

