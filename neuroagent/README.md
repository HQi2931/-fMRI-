# NeuroAgent

NeuroAgent 是一个通用科研 Agent 框架骨架。当前阶段只定义目录边界、模块职责和架构设计，不实现真实 Agent 推理循环、工具执行、RAG、数据库、领域分析或生产运行时。

未来领域能力应通过插件接入，而不是写入框架核心。框架核心只保留 Agent、Task、Message、Model、Strategy、Tool、Policy、Workflow、Approval、Memory、Retrieval、Context、Skill、Executor、Artifact、Event、Trace、Plugin 等通用概念。

## 当前状态

- 已建立单包模块化目录结构。
- 已为主要模块创建 `README.md` 和 `__init__.py`。
- 已提供架构设计文档：[NeuroAgent Framework Architecture](../docs/architecture/neuroagent-framework-architecture.md)。
- 尚未实现任何真实运行逻辑、外部服务连接、领域插件或生产配置。

## 模块导航

| 模块 | 职责 |
| --- | --- |
| [core](core/README.md) | Agent、Message、Task、Model、Strategy 与 Runtime 的核心抽象。 |
| [tools](tools/README.md) | 工具注册、schema、策略约束和工具运行时边界。 |
| [workflow](workflow/README.md) | 工作流定义、状态机、审批和状态变更规则。 |
| [memory](memory/README.md) | Working、Episodic、Semantic 与 Procedural 记忆的抽象边界。 |
| [retrieval](retrieval/README.md) | 文档摄取、分块、索引、搜索、重排和证据返回边界。 |
| [context](context/README.md) | Gather、Select、Structure、Compress 和上下文快照。 |
| [skills](skills/README.md) | 可加载技能的注册、加载和 schema 描述。 |
| [execution](execution/README.md) | Job、Executor、Sandbox 和 Artifact 的执行边界。 |
| [multi_agent](multi_agent/README.md) | Supervisor、Sequential Handoff 和多 Agent 协调边界。 |
| [observability](observability/README.md) | Event、Trace、Metric 和 Audit 记录边界。 |
| [plugins](plugins/README.md) | 插件注册、发现和加载边界。 |
| [infrastructure](infrastructure/README.md) | LLM、持久化、向量存储、对象存储和事件总线适配层。 |

## 后续开发入口

1. 先冻结核心接口草案，再实现最小可测试运行时。
2. 先使用 Mock Model、Mock Tool 和内存事件日志验证 Agent Runtime。
3. 通过 WorkflowEngine 管理状态变更，Agent 只提交变更请求。
4. 在框架 MVP 达到测试标准后，再接入第一个领域插件。

