# context

## 模块职责

`context` 负责 Gather、Select、Structure、Compress 流程，把任务、消息、记忆、证据、工作流状态和产物引用整理成可审计的 ContextPacket 与 ContextSnapshot。

## 模块边界

本模块不调用模型，不执行工具，不直接写记忆，也不绕过 token budget。上下文只携带必要信息，大文件只能以 ArtifactRef 摘要或引用进入。

## 依赖关系

`context` 由 `core` 的 AgentRuntime 调用；它从 `memory`、`retrieval`、`workflow`、`execution` 和历史消息中收集材料，并通过 `observability` 记录 ContextBuilt。

## 当前阶段

`interfaces.py` 已定义 `ContextMessage`、`PinnedContext`、`ContextPacket` 和 `ContextManager`。
Phase 1 的最小构建器只组装调用方提供的 recent messages、retrieval evidence 和 pinned context；
尚未实现 token budget、摘要、压缩或 pinned context 自动提取。

## 后续核心接口

- `ContextEngine`
- `ContextPacket`
- `ContextSnapshot`
- `Gatherer`
- `Selector`
- `Structurer`
- `Compressor`
- `TokenBudget`
