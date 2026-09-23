# API 层

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-09-23
- 关联：[API v1 契约](../../docs/api/api-v1.md)、[系统设计](../../docs/architecture/system-design.md)

`neuroagent.api` 是 FastAPI 传输层：负责 HTTP、请求校验、错误映射、SSE 和生产静态前端入口。

当前能力：

- `create_app()` 装配路由、统一错误处理与 `X-Trace-ID`；
- 业务错误、请求校验错误、未匹配路径和意外 `500` 统一返回 `{"error": {"code", "message", "details", "trace_id"}}`；
- SSE 通过 `/runs/{id}/events` 提供可续传的运行事件；
- `static.py` 在生产构建下提供前端入口。

本层不包含 fMRI 业务规则，不直接启动 MATLAB，不拼接系统命令，也不直接访问数据库。所有业务动作经应用服务层；公共契约以 [API v1 契约](../../docs/api/api-v1.md) 与 `web/openapi.json` 为准。