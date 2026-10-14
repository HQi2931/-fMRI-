# API 层

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-10-11
- 关联：[API v1 契约](../../docs/api/api-v1.md)、[系统设计](../../docs/architecture/system-design.md)

`neuroagent.api` 是 FastAPI 传输层：负责 HTTP、请求校验、错误映射、SSE 和生产静态前端入口。

当前能力：

- Work 提供独立的方案审查与确认接口，产物提供按编号校验的下载和有限图像预览；契约与阶段验证边界见 [API v1](../../docs/api/api-v1.md)。
- 会话提供针对精确 intent ID 的分析 outbox 人工重试入口；它只重新排队可恢复的 proposal-only 草稿准备，依赖 `Idempotency-Key`，不确认统计方法或启动统计运行。

- `routers/` 按环境、工作区、Agent 对话、Literature、项目、方案、Skill、统计、外部导入、运行与 Artifact 切分 HTTP 路由；`routes.py` 只聚合子路由并保留迁移期导出；
- handler 通过按能力重载的 `capability_from()` 解析具体应用服务类型；全部 JSON 请求体使用 `api/schemas/` 中独立的 HTTP DTO，并由 `to_application_message()` 显式映射到应用命令；响应模型由 `api/responses/` 独立拥有，并由 `to_http_response()` 映射应用结果；
- `create_app()` 装配路由、统一错误处理与 `X-Trace-ID`；
- 业务错误、请求校验错误、未匹配路径和意外 `500` 统一返回 `{"error": {"code", "message", "details", "trace_id"}}`；
- SSE 通过 `/runs/{id}/events` 提供可续传的运行事件；
- 对话回合通过 `stream=true` 提供正文 SSE，契约见 [API v1](../../docs/api/api-v1.md#持久化对话与工具编排)；
- `static.py` 在生产构建下提供前端入口。

本层不包含 fMRI 业务规则，不直接启动 MATLAB，不拼接系统命令，也不直接访问数据库。所有业务动作经应用服务层；公共契约以 [API v1 契约](../../docs/api/api-v1.md) 与 `web/openapi.json` 为准。
