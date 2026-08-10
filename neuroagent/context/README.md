# context

## 模块职责

`context` 负责 Gather、Select、Structure、Compress 流程，把任务、消息、记忆、证据、工作流状态和产物引用整理成可审计的 ContextPacket 与 ContextSnapshot。

## 模块边界

本模块不调用模型，不执行工具，不直接写记忆，也不绕过 token budget。上下文只携带必要信息，大文件只能以 ArtifactRef 摘要或引用进入。

## 依赖关系

`context` 由 `core` 的 AgentRuntime 调用；它从 `memory`、`retrieval`、`workflow`、`execution` 和历史消息中收集材料，并通过 `observability` 记录 ContextBuilt。

## 当前阶段

仅建立 gatherers、selectors、structure、compressors 和 snapshots 的目录边界，未实现上下文构建器。

## 后续核心接口

- `ContextEngine`
- `ContextPacket`
- `ContextSnapshot`
- `Gatherer`
- `Selector`
- `Structurer`
- `Compressor`
- `TokenBudget`
