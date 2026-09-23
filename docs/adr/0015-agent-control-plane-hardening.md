# ADR 0015：Agent 控制面预算、作用域与一次性执行授权

- 状态：Accepted
- 文档类型：ADR
- 维护者：system_architect
- 决策日期：2026-09-23
- 关联：[计划 0009](../plans/0009-agent-control-plane-hardening.md)、[ADR 0014](0014-bounded-tool-loop.md)
- 替代关系：补充并取代 ADR 0014 中关于循环“硬墙钟”、回退审计和执行确认边界的不足描述；保留其 READ/PROPOSE/EXECUTE 分级与 EXECUTE 目录过滤决定。

## 背景

ADR 0014 建立了 Agent 工具分级和有界循环，但后续审查发现，工具超时字段尚未真正执行，Provider/工具调用期间无法强制结束，循环回退可能丢失已执行工具的审计，Work 工具未按项目绑定，且执行确认同时存在精确口令和客户端布尔值两套语义。

## 决定

1. 循环预算以实际选中模型的上下文窗口为上限，并在每个 Provider/工具调用前检查剩余墙钟、上下文和取消状态。
2. 异步模型/工具调用使用可强制的 deadline；同步模型可见工具仅限只读或幂等操作，不把长时间 MATLAB 工作暴露给 Agent dispatcher。
3. 循环产生的所有工具 outcome，无论最终回答是否为空、非法或触发单次回退，都必须与会话消息在同一收口事务中保存。
4. Work 的项目型工具按请求级 conversation/project/run 作用域构建；越界对象对模型统一表现为不可用。
5. 真实执行统一使用服务端签发、绑定 conversation/project/plan hash/QC 选择、短时有效且单次消费的 ConfirmationChallenge。`real_execution_confirmed` 不能独立授权。
6. Challenge 消费、计划批准和运行创建在同一应用事务内完成，并继续保留 Worker 的环境、冻结计划、路径和 Artifact 谱系纵深校验。

## 后果

- Agent 的“硬预算”成为可观测且可测试的运行时约束，而不只是配置语义。
- 回退路径可能产生更多审计行，但审计不再依赖模型是否遵守 JSON 合同。
- UI 和自然语言确认需要统一适配 challenge；旧布尔字段进入弃用迁移期。
- 当前单用户本机运行不引入 RBAC；项目作用域是数据完整性和未来多用户迁移的前置边界，不等同于身份认证。
- 现有 READ/PROPOSE/EXECUTE 分级、模型不可见 EXECUTE 工具和 Worker 执行入口保持不变。

## 取舍

- 不使用线程杀死同步函数作为通用取消机制，因为 Python 线程无法安全终止任意底层调用；这类操作只能保持只读/幂等并设置外层 deadline。
- 不把确认 token 放入模型上下文；模型只能生成展示文本，challenge 由服务端保存并由用户交互消费。
- 不引入分布式锁或消息队列；SQLite 应用事务与现有幂等租约足以支持当前本机范围。
