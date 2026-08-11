# API v1 契约

所有业务接口位于 `/api/v1`。当前路由的机器事实来源是 [OpenAPI 文档](../../web/openapi.json)；运行中的同一契约可从 `/api/v1/openapi.json` 读取。

## 通用规则

- 所有 `POST` 请求要求 `Idempotency-Key` 头。key 的作用域包含具体操作；同一作用域内，相同 key 只能绑定规范化后的同一请求体。
- 修改已有资源时，请求体会携带 `expected_*_version`、计划哈希或 revision 哈希。不匹配时失败关闭，不自动覆盖。
- 业务错误、请求校验、框架 HTTP 错误、未匹配 API 路径和意外 `500` 都返回 `{"error": {"code", "message", "details", "trace_id"}}`；意外错误不会返回原始异常文本。每个 HTTP 响应都有 `X-Trace-ID`，客户端仍应先检查 HTTP 状态码。
- 下表中的 Artifact 接口只返回元数据、相对路径、校验和与 provenance。当前没有 Artifact 文件下载接口。

幂等请求按以下规则恢复：

| 服务端记录 | 响应/行为 | 客户端要求 |
| --- | --- | --- |
| 首次请求或已过期的 `pending` 租约 | 当前调用取得有时限的处理所有权；外部 Provider 调用期间由所有者心跳续租。任何过期记录都可能被一个调用原子接管，系统无法仅凭租约区分进程崩溃与心跳延迟 | 请求内容不变时继续使用原 key，不要通过换 key 猜测前次是否提交 |
| `pending` 且租约仍有效 | `409 idempotency_request_in_progress` | 稍后使用相同 key 和完全相同的请求重试 |
| `completed` | 返回已保存的原响应，不重复业务写入或 Provider 调用 | 将其视为原请求结果，随后可清除本地 pending key |
| 相同 key、不同请求 | `409 idempotency_key_reused` | 不得用该 key 提交改变后的请求；修正请求后使用新 key |

网络中断、取消、`5xx`、`idempotency_request_in_progress`、`idempotency_lease_lost`、`idempotency_race` 或 `idempotency_completion_conflict` 都不能证明服务端或远端 Provider 未接受请求，客户端必须保留原 key。浏览器仅在当前标签页的 `sessionStorage` 保存“请求指纹 → 随机 key”，不保存请求体；成功或明确的业务 `4xx` 后清除对应记录。续租与所有权围栏会降低并发接管和重复业务写入风险，但不能保证已经发往远端的请求不会被计费或执行。

## 系统和项目

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | API 和数据库健康状态 |
| `GET` | `/environment/probe` | MATLAB、SPM、DPABI 与适配器环境锁摘要 |
| `POST` | `/projects` | 创建项目并登记允许的源/工作根目录 |
| `GET` | `/projects` | 列出项目 |
| `GET` | `/projects/{project_id}` | 读取项目 |
| `GET` | `/projects/{project_id}/audit-events` | 按单调游标读取项目审计事件 |
| `POST` | `/projects/{project_id}/datasets` | 在项目允许根内登记数据集 |

## 数据、人口学和划分

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/datasets/{dataset_id}` | 读取数据集 |
| `POST` | `/datasets/{dataset_id}/inspect` | 只读扫描并创建 manifest revision |
| `GET` | `/manifests/{manifest_id}` | 读取冻结 manifest |
| `POST` | `/datasets/{dataset_id}/demographics/import` | 从允许源根内的 CSV/TSV/XLSX 路径导入人口学信息 |
| `GET` | `/demographics/{demographics_id}` | 读取人口学 revision 摘要 |
| `POST` | `/datasets/{dataset_id}/splits` | 创建受试者级划分 revision |
| `GET` | `/splits/{split_id}` | 读取数据集划分 |

`source_roots`、`work_root`、数据集 `source_path` 和人口学 `source_path` 是少数允许客户端提交本机路径的字段。服务端会规范化路径并校验父子边界；执行计划不接受任意输出路径。

## Skill 和审批

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/skills` | 列出已登记 SkillSpec |
| `POST` | `/skill-plans/resolve` | 从用户意图和服务端谱系编译 SkillPlan |
| `GET` | `/plan-revisions/{plan_revision_id}` | 读取计划 revision |
| `POST` | `/plan-revisions/{plan_revision_id}/approve` | 批准或拒绝精确计划哈希 |

浏览器只提交 `SkillPlanIntent`：项目、数据集、manifest hash 和显式科研参数。输入 Artifact lineage、基础 Cfg Artifact 与环境锁由服务端派生，客户端不能伪造。

## 运行、事件和 Artifact

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/runs` | 从已批准且当前有效的计划创建通用 Mock 运行 |
| `GET` | `/runs` | 按项目/状态列出运行 |
| `GET` | `/runs/{run_id}` | 读取运行 |
| `POST` | `/runs/{run_id}/cancel` | 提交显式取消原因 |
| `POST` | `/runs/{run_id}/retry` | 在已批准重试预算内重试 |
| `POST` | `/runs/{run_id}/diagnosis` | 对受限日志片段执行本地确定性失败分类，不自动修复或重跑 |
| `GET` | `/runs/{run_id}/events` | SSE 事件；支持 `after_event_id`、`Last-Event-ID` 和 `once=true` |
| `GET` | `/runs/{run_id}/artifacts` | 列出运行的 Artifact 元数据 |
| `GET` | `/artifacts/{artifact_id}` | 读取单个 Artifact 元数据 |

公共 `/runs` 当前只创建通用 Mock 作业，不会启动 MATLAB，也不会生成 ALFF/fALFF、ReHo 等真实指标图。

## 扩展分析预览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/ml/datasets/inspect` | 在项目允许根内只读检查 CSV/TSV/XLSX |
| `POST` | `/ml/templates` | 生成待用户批准的固定 Python 机器学习模板 |
| `POST` | `/roi/extractions/validate` | 校验 ROI 提取参数与结构化长宽表合同 |
| `POST` | `/organization/previews` | 构建不修改源文件的 DPABI 整理复制预览 |
| `POST` | `/cluster-localizations` | 用用户提供的 atlas 坐标标签匹配 cluster 峰值 |
| `POST` | `/agent/rsfmri/questions` | 使用本地证据回答限定范围的 rs-fMRI 问题 |

这些接口不接受自由 MATLAB/Python/Shell 文本。真实 ROI 执行、文件复制、ML 训练、NIfTI atlas 采样和联网检索仍需后续的审批工作流与受控 Tool。

## QC 和统计设计

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/qc-reviews` | 从类型化指标 Artifact 创建不可变 QC revision |
| `GET` | `/qc-reviews/{review_revision_id}` | 读取 QC revision |
| `POST` | `/qc-reviews/{review_revision_id}/approve` | 批准精确 QC hash 和冻结受试者顺序 |
| `POST` | `/statistical-designs` | 创建统计设计 revision |
| `GET` | `/statistical-designs/{plan_revision_id}` | 读取统计设计 |
| `POST` | `/statistical-designs/{plan_revision_id}/validate` | 校验设计并进入待审批 |
| `POST` | `/statistical-designs/{plan_revision_id}/approve` | 批准统计设计哈希 |
| `GET` | `/corrections` | 列出 FDR/GRF 校正能力和 schema |
| `POST` | `/statistics/runs` | 从已批准设计创建统计 Mock 运行 |
| `GET` | `/statistics/results?project_id=...&run_id=...` | 按项目（可选运行）列出已登记的统计结果摘要 |
| `GET` | `/statistics/results/{result_id}` | 读取冻结结果清单与 Markdown/JSON 复现报告 |

`/statistics/runs` 当前只排队 `statistics_mock`。它不调用 DPABI 统计函数，不产生统计图、效应量、簇表或可复现报告。统计运行完成后，明确标记为合成的复现报告可经 `/statistics/results` 查询；真实执行器产物登记仍待接入。

## Provider 和 Agent

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/model-profiles` | 创建只引用本地密钥环境变量名的 Profile |
| `GET` | `/model-profiles` | 列出 Profile |
| `GET` | `/model-profiles/{profile_id}` | 读取 Profile |
| `POST` | `/providers/test` | 发起一次脱敏的轻量连通性/schema smoke |
| `POST` | `/agent/tasks` | 执行结构化 Agent 任务 |
| `GET` | `/agent/tasks/{task_id}` | 读取 Agent 任务和路由决定 |

Agent 只返回结构化建议和未解决问题，不返回 Workflow transition 或可执行命令。外发上下文先经过失败关闭的脱敏策略；配置步骤见 [Provider 配置与 smoke](../operations/provider-setup.md)。
