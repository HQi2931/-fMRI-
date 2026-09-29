# Chat 与 Work 对话流式响应

- 状态：Implemented
- 文档类型：Plan
- 维护者：backend_service_engineer
- 创建日期：2026-09-29
- 更新日期：2026-09-29
- 目标里程碑：两种模式的正文增量展示与可靠收口
- 关联：[API 契约](../api/api-v1.md)、[ADR 0019](../adr/0019-conversation-streaming.md)

## 范围

沿用对话 POST 接口，通过 `stream=true` 提供 SSE。接入 Provider 原生流式文本与工具轮次，前端即时展示用户消息及回答增量。Work 仅预览 JSON 中的顶层 answer，完整校验后才处理草稿、提案及授权。

## 实施顺序

1. 实现请求隔离的正文观察器与 OpenAI 兼容流解析。
2. 在现有幂等操作外提供 SSE，最终消息落库后发送 final。
3. 前端接收 UTF-8 分块并将最终会话替换临时消息。
4. 验证真实增量、JSON 隔离、工具调用、失败、断连及幂等重放。

## 验收标准

- Chat 与 Work 均能在 Provider 完成前展示正文增量。
- 引用、方案、工具步骤以已校验并落库的 final 为准。
- Provider 回退与工具轮次通过 reset 隔离，正文不串联。
- 断连不会重复派发已受幂等保护的请求；同键可恢复已完成响应。
- 原非流式 API、科学校验与逐次执行确认保持有效。
- 合成 Provider/API/前端测试及相关静态检查通过；不调用真实 Provider 或 MATLAB。
