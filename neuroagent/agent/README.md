# Agent 层

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-09-23
- 关联：[系统设计](../../docs/architecture/system-design.md)、[ADR 0014](../../docs/adr/0014-bounded-tool-loop.md)

`neuroagent.agent` 是 Provider 中立的模型边界：它只做路由、脱敏、结构化输出和受控工具循环，不决定科研参数，也不执行任何工作。

当前能力：

- `ModelGateway` 按 `ModelProfile` 与 `TaskType` 路由，Provider 通过 OpenAI 兼容接口调用；
- `OutboundContextPolicy` 在出站前做失败关闭的去标识化，密钥与受试者信息不进入请求；
- 工具按 `READ`/`PROPOSE`/`EXECUTE` 三级声明权威，EXECUTE 工具在目录构建阶段即被丢弃；
- `loop.py` 实现有界工具循环：步数、墙钟、token 三重预算与重复失败熔断，停止原因是显式枚举；
- 工具结果经 `redact_tool_result()` 处理并标记为不可信数据后才回注上下文。
- Work 提示词采用 Plan-and-Execute：先说明输入事实、流程分支、参数建议及依据和待确认项，再交由现有草稿审查与执行确认链路处理。ALFF/fALFF 与 ReHo 按各自检查点规划；提示词不新增执行能力，也不代替服务端科学校验。
- 对话已确认的 DPABI 起始阶段会作为当前选择传入；模型重复推荐同一阶段时继续生成草稿，只有未确认或建议切换阶段时才要求阶段确认。
- Work 可通过只读 `get_run_diagnosis` 工具读取失败 attempt 的有限日志分类结果；工具只返回分类、摘要和建议，不传递日志证据文本，不重试运行或修改科研参数。

本层不写数据库、不推进工作流状态、不持有审批权限。未声明 `ModelCapability.TOOL_USE` 的模型走原有单次调用路径。Agent 工具的是非权威集合由 `neuroagent/application/work_actions.py` 单一维护。
