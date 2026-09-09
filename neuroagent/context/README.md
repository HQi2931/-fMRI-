# context

## 模块职责

`context` 负责 Gather、Select、Structure、Compress 流程，把任务、消息、记忆、证据、工作流状态和产物引用整理成可审计的 ContextPacket 与 ContextSnapshot。

## 模块边界

本模块不调用模型，不执行工具，不直接写记忆，也不绕过 token budget。上下文只携带必要信息，大文件只能以 ArtifactRef 摘要或引用进入。

## 依赖关系

`context` 由 `core` 的 AgentRuntime 调用；它从 `memory`、`retrieval`、`workflow`、`execution` 和历史消息中收集材料，并通过 `observability` 记录 ContextBuilt。

## 当前阶段

`ContextEngine` 已按模型窗口为当前问题、最近完整问答轮次、检索证据、确认记忆、滚动摘要和 Work 状态分配预算。Chat 与 Work 共用同一选择逻辑，Gateway 在发送前统一脱敏；每次成功构建保存不含完整正文的结构化快照。

长对话在未摘要历史达到 24 条消息或超过对话预算阈值时压缩，保留最近 6 个完整问答轮次。Chat 优先调用当前模型生成摘要，失败时使用确定性摘录；Work 使用确定性摘录。

## 后续核心接口

- Provider 专用 token 计数器
- 更细粒度的上下文调试视图
