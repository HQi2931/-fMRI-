# memory

## 模块职责

`memory` 定义 Working Memory、Episodic Memory、Semantic Memory 和 Procedural Knowledge 的模型、仓储、策略和服务边界。

## 模块边界

本模块不把未经验证的信息升级为事实，不跨项目串联记忆，不直接参与模型决策，也不替代检索证据。已验证记忆的修改必须经过策略和审计。

## 依赖关系

`memory` 被 `context` 召回并包装成上下文输入；可依赖 `infrastructure/persistence` 做存储适配，依赖 `observability` 记录 MemoryRecalled 和记忆更新事件。

## 已实现行为

SQLite 已保存会话/项目范围的 `memory_records`。允许列表内的语言、篇幅和格式偏好可以自动确认；频段等科学参数只生成待确认候选。用户可通过 Conversation Context API 确认、修改、拒绝、固定或忘记记忆。

确认记忆可进入上下文，项目记忆只在同一项目内共享。已审批计划、运行、QC 和产物状态不复制为记忆，始终从业务表实时读取。“忘记”会清空正文并保留审计墓碑。

## 后续核心接口

当前开发中的语义召回由 `SemanticMemoryService` 编排：SQLite 保存绑定记忆版本和
embedding 模型标识的向量，`SemanticMemoryRanker` 融合向量与关键词排名，并按重要性和
时间衰减排序。Chat 已接入召回，同项目会话共享项目记忆；没有配置 embedding 时明确降级为关键词。

`POST /conversations/{conversation_id}/context/index` 显式建立或重试索引。设置项
`memory_embedding_profile` 使用 ModelProfile 格式，配置独立 embedding 模型及密钥环境变量；
同时需要配置脱敏盐。调用发生在数据库写事务之外，保存时检查记忆版本，修改和忘记清除旧向量。

创建和修改支持 `importance`（0–1）与带时区的 `expires_at`。过期记忆仍可在管理列表查看，
但不参与召回或新建索引。更新显式传 `expires_at: null` 清除有效期，省略字段保留原值。

Chat 会从带持久化模型标识和内容版本的向量中执行语义与关键词融合召回，并按固定状态、
重要性和时间衰减排序。会话范围严格隔离；项目范围只在绑定到同一项目的会话间共享。
未配置或暂时无法访问 embedding 服务时，会明确退回关键词召回。

自动提取只读取当前用户消息并保存来源：语言、篇幅等白名单偏好可规则确认；模型提取的
项目事实、决定、指令和科研参数先进入待确认状态。同作用域同键的新值成为冲突建议，用户
必须采用、合并或保留原值，系统不会静默覆盖已确认内容。

前端支持新建和切换会话、会话/项目范围、重要性、到期日、搜索、确认、固定、修改、忘记、
冲突解决以及显式重试语义索引。忘记会清空记忆正文和向量、使覆盖其来源的摘要失效，并在
后续模型上下文中过滤来源消息；原始聊天记录仍作为独立审计历史保留。

记忆只作为用户背景与约束，不替代论文证据、已审批计划、运行状态或 QC 结果。
完整验收合同见 `docs/plans/0008-semantic-long-term-memory.md`。
