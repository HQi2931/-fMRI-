# observability

## 模块职责

`observability` 负责 RuntimeEvent、Trace、Metric 和 Audit 记录，使上下文构建、记忆召回、证据检索、工具执行、审批、状态变更和产物创建都可追踪。

## 模块边界

本模块不执行业务逻辑，不改变工作流状态，不保存敏感上下文原文到不合规位置，也不替代错误处理。

## 依赖关系

所有运行时模块都可以写入事件；底层可依赖 `infrastructure/event_bus` 和 `infrastructure/persistence` 的适配接口。

## 当前阶段

仅建立 events、tracing、metrics 和 audit 的目录边界，未实现事件总线或审计存储。

## 后续核心接口

- `RuntimeEvent`
- `EventBus`
- `TraceContext`
- `AuditRecord`
- `MetricRecord`

