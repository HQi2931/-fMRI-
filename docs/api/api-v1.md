# API v1 契约

所有业务接口位于 `/api/v1`。当前路由的机器事实来源是 [OpenAPI 文档](../../web/openapi.json)；运行中的同一契约可从 `/api/v1/openapi.json` 读取。

## 通用规则

- JSON 业务写入 `POST` 请求要求 `Idempotency-Key` 头。key 的作用域包含具体操作；同一作用域内，相同 key 只能绑定规范化后的同一请求体。Phase 1 的 multipart PDF 上传是例外，客户端在响应不确定时应先按 SHA-256/文献列表核对，不要盲目重复上传。
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
| `POST` | `/workspaces/check` | 对用户选择的本机目录执行只读 DPABI/fMRI 格式检查 |
| `POST` | `/workspaces/pick` | 打开服务所在 Windows 会话的系统文件夹选择器 |
| `POST` | `/projects/{project_id}/datasets` | 在项目允许根内登记数据集 |

## 持久化对话与工具编排

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/conversations` | 创建 `chat` 或 `work` 对话并持久化欢迎消息 |
| `GET` | `/conversations?mode=...` | 按更新时间读取对话、消息和工具调用记录 |
| `GET` | `/conversations/{conversation_id}` | 恢复一段完整多轮对话 |
| `POST` | `/conversations/{conversation_id}/turns` | 保存一轮消息，执行本地工具或经脱敏策略调用 Chat 模型 |

`chat` 先调用本地 `rag_rsfmri_question`；存在模型配置时，将限定范围的问题和本地证据通过
`OutboundContextPolicy` 脱敏后交给所选 LLM。请求中的 `allow_remote_search=true` 只会路由到
声明 `web_search` 能力的 Profile，并将 URL 引用、模型 Profile、上下文哈希和用量摘要保存到
`rsfmri_chat_llm` 工具记录。没有模型配置且未要求联网时保留纯本地 RAG 回退。`work` 当前可编排
`check_workspace`、`get_run_progress` 和 `start_dpabi_preprocessing`。每次工具调用都会保存
输入摘要、状态、输出或安全错误。启动预处理必须提交已审批计划 ID、计划哈希和
`real_execution_confirmed=true`；服务只负责排队，实际 MATLAB 仍由现有 Worker 执行。

前端把 Model Profile 作为服务商 API 连接使用。绑定时调用 `/providers/models` 验证密钥并获取模型；
Agent 页面再次读取该连接的模型列表，并在每个对话回合用 `model` 字段提交用户实际选择的模型。
Profile 内部保存的 `model` 仅作为服务商暂时无法列出模型时的兼容回退，不要求用户额外维护。

Chat 回合请求从 Phase 1 起包含 `stream` 字段。当前只支持 `false`；`true` 返回
`422 chat_streaming_not_implemented`，为后续 SSE/NDJSON token stream 保留明确扩展位。ChatAgent 会把
执行型语言转换为 `work_request` draft 并保存到消息 payload，但不会创建计划、运行 MATLAB 或调用
Work Mode。普通回答中的 citation 只由类型化检索证据或 Provider 返回的 URL annotation 生成，
不会从模型自由文本中猜测来源。

## Literature 文献管理

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/literature/papers` | multipart 上传一篇 PDF，返回 Paper、sections、chunks 和解析 warnings |
| `GET` | `/literature/papers` | 列出已摄取论文及其结构化结果 |
| `GET` | `/literature/papers/{paper_id}` | 读取 Paper、sections 和可追溯 chunks |

PDF 原文件保存在 `allowed_work_root/literature/{paper_id}/source.pdf`，API/SQLite 只保存该规范相对路径。
默认大小上限 50 MiB。解析使用 `pypdf` 逐物理页抽取文本；无文本层、加密、损坏、伪 PDF 和超限文件
返回稳定错误，不会导致服务退出。Section detector 使用可扩展别名规则识别 Abstract、Introduction、
Methods 及 rs-fMRI 常见 Methods 子节、Results、Discussion、Conclusion、References；无法识别时保留为
Front Matter。Chunker 在 section 内按 paragraph/sentence 聚合，默认约 600 tokens、100 overlap，
每个 chunk 持久化 `paper_id`、section/subsection、物理页范围、token count、index 和 rs-fMRI metadata
占位。当前不会为这些 chunks 生成 embedding 或建立检索索引。

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
| `POST` | `/runs` | 从已批准且当前有效的计划创建 Mock 或逐次确认的 MATLAB 运行 |
| `GET` | `/runs` | 按项目/状态列出运行 |
| `GET` | `/runs/{run_id}` | 读取运行 |
| `POST` | `/runs/{run_id}/cancel` | 提交显式取消原因 |
| `POST` | `/runs/{run_id}/retry` | 在已批准重试预算内重试 |
| `POST` | `/runs/{run_id}/diagnosis` | 对受限日志片段执行本地确定性失败分类，不自动修复或重跑 |
| `GET` | `/runs/{run_id}/events` | SSE 事件；支持 `after_event_id`、`Last-Event-ID` 和 `once=true` |
| `GET` | `/runs/{run_id}/artifacts` | 列出运行的 Artifact 元数据 |
| `GET` | `/artifacts/{artifact_id}` | 读取单个 Artifact 元数据 |

`/runs` 默认创建 Mock 作业。显式选择 `execution_backend=matlab` 时，还必须满足本机执行开关、
环境锁、计划审批和逐次确认。`workspace_mode=in_place` 仅接受 DPABI-ready 数据集；Worker 把该数据集
目录传给 `DPARSFA_run` 作为 `WorkingDir`，DPABI stage/Results 留在所选工作区，脚本、日志、
证据和登记产物仍保存在隔离 attempt 目录。

## 扩展分析预览

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/ml/datasets/inspect` | 在项目允许根内只读检查 CSV/TSV/XLSX |
| `POST` | `/ml/templates` | 生成待用户批准的固定 Python 机器学习模板 |
| `POST` | `/roi/extractions/validate` | 校验 ROI 提取参数与结构化长宽表合同 |
| `POST` | `/organization/previews` | 构建不修改源文件的 DPABI 整理复制预览 |
| `POST` | `/cluster-localizations` | 用用户提供的 atlas 坐标标签匹配 cluster 峰值 |
| `POST` | `/agent/rsfmri/questions` | 使用本地证据回答限定范围的 rs-fMRI 问题 |

这些接口不接受自由 MATLAB/Python/Shell 文本。真实 ROI 执行、文件复制、ML 训练和 NIfTI atlas 采样仍需后续的审批工作流与受控 Tool。联网搜索仅在 Chat 回合显式开启，并由具备相应能力的模型 Provider 执行。

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
| `POST` | `/model-profiles` | 绑定或更新只引用本地密钥环境变量名的服务商连接（可携带 `api_key` 写入本地 `.env`） |
| `GET` | `/model-profiles` | 列出已绑定服务商 |
| `GET` | `/model-profiles/{profile_id}` | 读取服务商连接 |
| `DELETE` | `/model-profiles/{profile_id}` | 解除服务商连接 |
| `POST` | `/providers/models` | 列出某个 OpenAI 兼容 Provider 的可用模型（传 base_url + api_key 或密钥环境变量名） |
| `POST` | `/providers/test` | 发起一次脱敏的轻量连通性/schema smoke |
| `POST` | `/agent/tasks` | 执行结构化 Agent 任务 |
| `GET` | `/agent/tasks/{task_id}` | 读取 Agent 任务和路由决定 |

Agent 只返回结构化建议和未解决问题，不返回 Workflow transition 或可执行命令。外发上下文先经过失败关闭的脱敏策略；配置步骤见 [Provider 配置与 smoke](../operations/provider-setup.md)。
