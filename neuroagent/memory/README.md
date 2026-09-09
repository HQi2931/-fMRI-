# memory

## 模块职责

`memory` 定义 Working Memory、Episodic Memory、Semantic Memory 和 Procedural Knowledge 的模型、仓储、策略和服务边界。

## 模块边界

本模块不把未经验证的信息升级为事实，不跨项目串联记忆，不直接参与模型决策，也不替代检索证据。已验证记忆的修改必须经过策略和审计。

## 依赖关系

`memory` 被 `context` 召回并包装成上下文输入；可依赖 `infrastructure/persistence` 做存储适配，依赖 `observability` 记录 MemoryRecalled 和记忆更新事件。

## 当前阶段

SQLite 已保存会话/项目范围的 `memory_records`。允许列表内的语言、篇幅和格式偏好可以自动确认；频段等科学参数只生成待确认候选。用户可通过 Conversation Context API 确认、修改、拒绝、固定或忘记记忆。

确认记忆可进入上下文，项目记忆只在同一项目内共享。已审批计划、运行、QC 和产物状态不复制为记忆，始终从业务表实时读取。“忘记”会清空正文并保留审计墓碑。

## 后续核心接口

- 语义记忆召回
- 可配置的记忆过期策略
