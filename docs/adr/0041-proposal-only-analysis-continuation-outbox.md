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

仅对具有稳定 `analysis_dependency.task_id` 的显式复合请求创建 `AnalysisProposalOutbox`。intent ID 按 conversation、请求内容和 HTTP 幂等键确定；同一幂等键重放稳定，不同会话即使项目与请求文本相同也不会冲突。ID 沿 active conversation intent envelope 与 `work_draft` 的补参、审查和重新编译路径传递，并在冻结 Plan 创建 preprocessing WorkTask 时同时写入 Task/TaskStep 引用及原始请求 hash。运行启动只从该任务引用读取 intent，再按精确 ID、conversation、project 和 hash 查验正文；不会按项目扫描历史消息。新普通预处理目标会清除 active envelope/draft 上的 intent 关联；同一未启动 Task 被普通目标复用时也会原子清除 Task/TaskStep 引用，已启动任务拒绝替换关联。已消费草稿和 cleared envelope 不会给新任务续传关联。请求正文只通过 conversation 中精确匹配 intent ID 的消息读取。项目、Plan、Run 或任务绑定冲突时拒绝。

创建时状态为 `pending`。只有预处理任务通过现有冻结目标 Artifact 完整核验后，才在同一 SQLite Unit of Work 中，将预处理任务完成并把 outbox 转为 `ready`，同时保存已核验 Artifact ID 的精确有序集合。预处理专用请求不创建 outbox。READY 项可用短租约领取；每次重领递增 fencing token，旧租约或旧 token 无法再通过 claim 校验。

本 ADR 建立 proposal-only 持久合同和 READY outbox。当前纵向切片已实现独立 Worker 消费：它先重新核验 conversation/project、intent ID/hash、预处理 WorkTask、冻结 Plan/manifest、Run、QC、目标 Artifact 与 lineage，再在事务外调用现有 Work 分析 Agent 准备统计草稿。outbox 仅绑定本次预处理的目标 Artifact；lineage 所需且另行核验的 mask 作为依赖 ID 单独保留，并与目标 ID 共同组成统计装配器的精确 allowlist。

模型上下文只接收已核验来源的有界摘要，包括 QC 状态、指标类型与数量、纳入受试者数量和 mask 数量；逐 Artifact ID、路径、校验和及完整 lineage 保留在服务端。统计装配器使用服务端核验结果构造 allowlist，不接受模型提供的来源身份。会话页面按 outbox retry generation 区分在途和已完成续接，避免旧一代 `WAITING_USER` 消息终止新一代重试的轮询或保留过期重试按钮。

模型结果提交时，在同一 SQLite Unit of Work 中追加 assistant-only continuation、保存分析 WorkTask/提案绑定并终结 outbox；事务内必须匹配 claim fencing token，失效则整批回滚。重复消费只允许一条 continuation 和一份任务/提案。草稿缺少用户决策时以 `WAITING_USER` 提出具体问题；短暂错误使用有界退避，超过上限或模型不可用时提供显式重试入口。该 Worker 与现有 Workflow job/reconciliation 通道分开运行。它不会伪造 user message、创建或批准统计 Plan、创建统计 Run，也不会运行 MATLAB；分析方法与科研参数仍需用户审查。

当前 MVP 每个 preprocessing WorkTask 最多绑定一个分析 intent/outbox；同一任务的多项独立分析请求仍需后续扩展，当前不会覆盖或合并它们。

## 后果

页面关闭或进程重启后，可以从 intent ID 重新解析原始分析请求，并从 outbox 获取已经核验的输入引用。outbox 的 `ready` 只表示预处理证据齐备，可开始准备分析提案；它不代表统计方法已确定、设计已通过或计算已获授权。
