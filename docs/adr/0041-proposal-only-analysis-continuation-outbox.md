# ADR 0041：复合任务的提案专用分析续接 outbox

- 状态：Accepted
- 文档类型：ADR
- 维护者：backend_service_engineer
- 创建日期：2026-10-14
- 更新日期：2026-10-14
- 决策日期：2026-10-14
- 关联：[计划 0014](../plans/0014-general-agent-automation.md)、[ADR 0036：持久 Work 任务与运行状态边界](0036-persistent-work-task-boundary.md)、[ADR 0040：预处理任务以冻结目标产物完成](0040-preprocessing-task-output-completion.md)

## 背景

显式的 `preprocess_then_analyze` 请求可在会话 payload 中保留后续分析意图，但预处理 WorkTask 完成后没有持久的下游唤醒记录。后续恢复需要精确找回原请求，同时不能在 outbox 中再复制敏感正文，也不能越过统计方案审查与执行授权。

## 决策

仅对具有稳定 `analysis_dependency.task_id` 的显式复合请求创建 `AnalysisProposalOutbox`。记录绑定 intent ID、conversation、project、预处理 WorkTask、冻结 Plan revision、manifest hash、Run ID，以及原始 `analysis_request` 的内容哈希；请求正文只通过 conversation 中精确匹配 intent ID 的消息读取。唯一 intent ID 使重复创建幂等；项目、Plan、Run 或任务绑定冲突时拒绝。

创建时状态为 `pending`。只有预处理任务通过现有冻结目标 Artifact 完整核验后，才在同一 SQLite Unit of Work 中，将预处理任务完成并把 outbox 转为 `ready`，同时保存已核验 Artifact ID 的精确有序集合。预处理专用请求不创建 outbox。READY 项可用短租约领取；每次重领递增 fencing token，旧租约或旧 token 无法再通过 claim 校验。

本 ADR 只建立 proposal-only 持久合同和 READY outbox。它不消费 outbox、不调用模型、不伪造用户消息、不创建或批准统计 Plan，也不创建统计 Run。后续 Worker 消费和统计提案准备须另行实现，且只能停在用户审查/选择边界。

## 后果

页面关闭或进程重启后，可以从 intent ID 重新解析原始分析请求，并从 outbox 获取已经核验的输入引用。outbox 的 `ready` 只表示预处理证据齐备，可开始准备分析提案；它不代表统计方法已确定、设计已通过或计算已获授权。
