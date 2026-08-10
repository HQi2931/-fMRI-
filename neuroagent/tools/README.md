# tools

## 模块职责

`tools` 管理工具定义、工具注册、输入输出 schema、风险等级、权限策略和工具运行时边界。

## 模块边界

本模块不直接执行任意 Shell，不跳过审批，不修改工作流状态，也不保存最终产物。真实执行应委托给 `execution`，状态变更应经过 `workflow`。

## 依赖关系

`tools` 被 `core` 和 `workflow` 调用；它依赖 `execution` 执行受控 Job，依赖 `observability` 记录 ToolRequested、ToolCompleted 和失败事件。

## 当前阶段

仅建立注册、schema、policy 和 runtime 的目录边界，未实现真实工具调用。

## 后续核心接口

- `BaseTool`
- `ToolRegistry`
- `ToolSchema`
- `ToolPolicy`
- `ToolCall`
- `ToolResult`
- `ToolRuntime`

