# memory

## 模块职责

`memory` 定义 Working Memory、Episodic Memory、Semantic Memory 和 Procedural Knowledge 的模型、仓储、策略和服务边界。

## 模块边界

本模块不把未经验证的信息升级为事实，不跨项目串联记忆，不直接参与模型决策，也不替代检索证据。已验证记忆的修改必须经过策略和审计。

## 依赖关系

`memory` 被 `context` 召回并包装成上下文输入；可依赖 `infrastructure/persistence` 做存储适配，依赖 `observability` 记录 MemoryRecalled 和记忆更新事件。

## 当前阶段

`interfaces.py` 已定义 `MemorySnapshot` 和 `MemoryService`。Phase 1 仅使用 conversation 已持久化消息的
有界 recent window，不建立独立 memory 表，不做长期记忆、摘要、语义召回或 pinned context 自动更新。

## 后续核心接口

- `MemoryRecord`
- `MemoryScope`
- `MemoryService`
- `MemoryRepository`
- `MemoryPolicy`
- `MemoryUpdate`
