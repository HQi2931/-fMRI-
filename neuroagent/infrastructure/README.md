# infrastructure

## 模块职责

`infrastructure` 提供外部系统适配边界，包括 LLM provider、持久化、向量存储、对象存储和事件总线。上层模块只依赖接口，不绑定具体产品。

## 模块边界

本模块不包含业务规则，不决定 Agent 策略，不处理领域数据语义，也不在信息不足时锁定生产技术选型。

## 依赖关系

`core`、`memory`、`retrieval`、`execution` 和 `observability` 可通过接口调用基础设施适配层。依赖方向应从上层指向抽象接口，具体实现可由配置或插件注入。

## 当前阶段

仅建立 llm、persistence、vector_store、object_store 和 event_bus 的目录边界，未实现任何外部服务连接。

## 后续核心接口

- `ModelProvider`
- `PersistenceAdapter`
- `VectorStore`
- `ObjectStore`
- `EventBusAdapter`

