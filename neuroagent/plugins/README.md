# plugins

## 模块职责

`plugins` 负责插件发现、插件元数据、插件注册和插件加载边界。插件可以提供工具、工作流、schema、技能和领域实现。

## 模块边界

插件不能直接修改框架核心，不能绕过 ToolRegistry、PolicyEngine、WorkflowEngine 或 ApprovalGate，也不能把领域概念写入核心模块。

## 依赖关系

`plugins` 与 `skills`、`tools`、`workflow`、`retrieval` 和 `execution` 交互，为这些模块提供扩展声明和加载入口。

## 当前阶段

仅建立 registry 和 loader 的目录边界，未创建任何领域插件实现。

## 后续核心接口

- `PluginSpec`
- `PluginRegistry`
- `PluginLoader`
- `PluginContribution`
- `PluginPolicy`

