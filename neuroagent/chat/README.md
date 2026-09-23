# Chat 模块

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-09-23
- 关联：[Phase 1 设计](../../docs/development/rsfmri-chat-module-design.md)、[ADR 0012](../../docs/adr/0012-chat-work-only.md)

`neuroagent.chat` 实现 Chat 模式的方法学问答与文献检索编排。它只读、只解释，没有任何科研执行权限。

当前能力：

- `ChatAgent` 合并意图判断与追问改写，明确寒暄免检索；
- `evidence.py` 为检索证据分配编号，最终引用编号与工具返回的证据编号一致；
- `tool_loop.py` 在声明 `tool_use` 能力的模型上执行有界多跳检索，失败时回退单次路径；
- 引用按正文编号校验，无效 `[Cn]` 被剔除；旧索引缺失页码时明确为空，不推测论文页码。

本模块不写入业务状态、不批准计划、不启动运行。文献摄取属于 `neuroagent/literature`，模型调用经 `neuroagent/agent`，持久化经应用服务层。