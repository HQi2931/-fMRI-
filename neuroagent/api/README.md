# API 层

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-10-21
- 关联：[API v1 契约](../../docs/api/api-v1.md)、[系统设计](../../docs/architecture/system-design.md)

`neuroagent.api` 是 FastAPI 传输层：负责 HTTP、请求校验、错误映射、SSE 和生产静态前端入口。

当前能力：

- `POST /skill-plans/dry-run` 使用与 resolve 相同的请求 DTO，只读运行 Skill Resolver、Validator 和 Compiler；支持预处理及调用方显式指定已登记输入 Artifact 的 metric-only 请求。metric-only 沿用冻结 manifest、来源 Plan、Artifact lineage 和单主体检查，不自动选择来源；响应不探测执行环境，不创建 Plan/Approval/Run 或事件。Work 页面仍没有 metric-only Artifact picker。
- `GET /conversations/{conversation_id}/composite-goals` 返回服务端统一判断的 `analysis_selection_available`；选择入口覆盖等待审查、等待运行及已有恢复状态。`POST /conversations/{conversation_id}/composite-goals/{intent_task_id}/select` 按会话版本、幂等键和精确来源恢复未冻结草稿或冻结 Plan/Run，不批准或启动计算。目标指针保存在现有角色 JSON，其他目标的后台续接不抢占前台；冻结请求仍核验精确 `proposal_message_id`。见 [ADR 0046](../../docs/adr/0046-composite-goal-foreground-selection.md) 与 [ADR 0047](../../docs/adr/0047-frozen-analysis-goal-restoration.md)。

- Work 提供独立的方案审查与确认接口；多步骤任务另有只读 `GET /conversations/{conversation_id}/work-tasks/{task_id}/steps/{step_id}/review`，仅返回经持久任务、当前 READY 步骤和冻结 Plan 身份核验的审查 DTO，不批准 Plan、不签发执行授权或创建 Run。产物提供按编号校验的下载和有限图像预览；契约与阶段验证边界见 [API v1](../../docs/api/api-v1.md)。
- 会话提供针对精确 intent ID 的分析 outbox 人工重试入口；它只重新排队可恢复的 proposal-only 草稿准备，依赖 `Idempotency-Key`，不确认统计方法或启动统计运行。
- 会话提供独立的统计结果解释 outbox 重试入口；它只重新排队同一真实 StatisticalResult 的 assistant-only 解释草稿，依赖 `Idempotency-Key`，不创建或批准 Plan/Run。
- Work 会话提供 conversation-scoped 复合目标进度列表；响应通过每个稳定 intent ID 重读权威 snapshot 投影，不接受裸 intent ID 查询。

- `routers/` 按环境、工作区、Agent 对话、Literature、项目、方案、Skill、统计、外部导入、运行与 Artifact 切分 HTTP 路由；`routes.py` 只聚合子路由并保留迁移期导出；
- handler 通过按能力重载的 `capability_from()` 解析具体应用服务类型；全部 JSON 请求体使用 `api/schemas/` 中独立的 HTTP DTO，并由 `to_application_message()` 显式映射到应用命令；响应模型由 `api/responses/` 独立拥有，并由 `to_http_response()` 映射应用结果；
- `create_app()` 装配路由、统一错误处理与 `X-Trace-ID`；
- 业务错误、请求校验错误、未匹配路径和意外 `500` 统一返回 `{"error": {"code", "message", "details", "trace_id"}}`；
- SSE 通过 `/runs/{id}/events` 提供可续传的运行事件；
- `GET /runs/{id}/diagnosis` 从当前 attempt 的本地日志实时生成确定性诊断；尚无日志返回空 evidence，不上传给模型；
- 对话回合通过 `stream=true` 提供正文 SSE，契约见 [API v1](../../docs/api/api-v1.md#持久化对话与工具编排)；
- `static.py` 在生产构建下提供前端入口。

本层不包含 fMRI 业务规则，不直接启动 MATLAB，不拼接系统命令，也不直接访问数据库。所有业务动作经应用服务层；公共契约以 [API v1 契约](../../docs/api/api-v1.md) 与 `web/openapi.json` 为准。
