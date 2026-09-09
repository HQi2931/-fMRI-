# rs-fMRI Chat Mode 模块分析

> 后续需求更新：用户要求直接复用 `D:/Project_for_agent/fMRIAnalysis` 的 RAG 并减少低价值测试。
> 下文为编码前的原始分析；“只预留 RAG / 不引入 Chroma”的阶段范围已被本更新替代。
> 已检查源项目的 retriever、DashScope embedding/reranker、chunk_indexer、MinerU pipeline 和 Chroma 索引。
> 复用前三个运行模块及已有索引副本，保留当前 PDF/section/chunk 页码链路，不移植 Work 功能。
> 旧库有 2,521 个 1,024 维片段，但无 paper_id/page metadata，不能冒充严格页码 Citation。
> 修复 RRF max、按标题去重、来源字段丢失和重复重排；通过现有 RagService 接入可选 rag 依赖组。
> 具体修改范围、配置和少量关键验证见实施报告。

- 日期：2026-09-07
- 范围：Chat Mode Phase 1（Milestone 1～3）
- 结论：在现有模块化单体上增量开发，不重写现有 Agent、对话、SQLite 或 Work 执行链路。

## 1. 当前项目架构

项目是 Windows 本地优先的模块化单体：React 前端调用 FastAPI `/api/v1`，应用服务编排领域规则和持久化，SQLite/Alembic 保存元数据，独立 Worker 执行获批任务。主要依赖方向是 `API -> Application -> Domain/Ports <- Infrastructure`。真实 MATLAB/DPABI 只能由 Worker 在审批和逐次确认后执行。

与本阶段最相关的目录如下：

```text
neuroagent/
├─ agent/                         模型 Profile、路由、Provider、脱敏和网关
├─ analysis/rag.py                当前本地文档关键词检索（不是完整 RAG）
├─ api/                           FastAPI 工厂、统一错误映射、REST/SSE 路由
├─ application/                   DTO、端口、幂等和用例编排
├─ infrastructure/persistence/    SQLite repository、SQLAlchemy row、Alembic
├─ observability/                 trace id 和事件脱敏
├─ retrieval/                     只有边界 README，未实现
├─ context/                       只有边界 README，未实现
└─ memory/                        只有边界 README，未实现
tests/
├─ agent/
├─ backend/
├─ integration/
└─ science/
```

## 2. 主要技术栈

- Python 3.11、FastAPI、Pydantic v2、pydantic-settings。
- SQLAlchemy 2、SQLite、Alembic。
- `httpx` 驱动 OpenAI-compatible Chat Completions Provider。
- React、TypeScript、Vite 前端。
- pytest、pytest-asyncio、pytest-cov；ruff、mypy strict。
- `python-multipart` 已存在，可复用为 PDF multipart 上传。

项目当前没有 PDF 文本抽取依赖、tokenizer、embedding 库或向量数据库客户端。

## 3. API、错误、日志与配置

- API 使用 `/api/v1` 前缀；写操作普遍使用 `Idempotency-Key`。
- `neuroagent/api/app.py` 将 `ApplicationError`、请求校验、HTTP 错误和未知异常映射为稳定 `ErrorResponse` envelope，并给每个请求绑定 `X-Trace-ID`。
- 长任务已有 SSE：`GET /api/v1/runs/{run_id}/events`。Chat 尚无 token streaming，但 Provider Profile 已有 `streaming` capability 枚举，可保留协议扩展位。
- 配置集中在 `application/settings.py`，从根目录 `.env` 读取 `RSFMRI_*`；配置模型冻结并有边界校验。
- Python 日志目前较少，API 未处理异常通过标准 `logging` 记录类型和 trace id；运行事件另有持久化审计模型。

## 4. 当前 Agent / Chat / LLM 实现

### 可复用部分

- `ModelProfile`、`ModelRouter`、`ModelProvider`、`ModelGateway` 已形成 LLM 抽象和 provider fallback。
- `OutboundContextPolicy` 在外部模型调用前脱敏并生成上下文哈希。
- `OpenAICompatibleProvider` 处理 timeout、网络错误、429/5xx、响应结构异常及 Provider URL citation。
- 当前工作树已增加持久化 Conversation、Message、ToolCall 表、repository、migration 和 API：
  - `POST /api/v1/conversations`
  - `GET /api/v1/conversations`
  - `GET /api/v1/conversations/{conversation_id}`
  - `POST /api/v1/conversations/{conversation_id}/turns`
- Chat 和 Work 对话由 `ConversationMode` 区分；消息按 `sequence` 持久化，刷新后可恢复。

### 当前问题

- Chat 请求的意图判断、当前本地检索、LLM 调用、citation 拼装和消息保存主要集中在 `ConversationMixin`，还没有独立 `ChatAgent`。
- 当前 `analysis/rag.py` 只对仓库内 Markdown/TXT/YAML 做 token-overlap 关键词搜索；它不是论文 RAG，也没有统一 Retriever/VectorStore/Reranker 接口。
- 当前对话会持久化消息，但 Chat 调用没有显式 ContextManager 或 MemoryService；历史消息尚未进入模型上下文。
- 当前 citation 包含本地文件名或 Provider URL，尚无基于 `chunk_id + paper_id + section + page` 的论文引用链路。

## 5. 当前持久化与迁移

- SQLite 是元数据事实来源，`Database.initialize()` 对文件库运行 Alembic，对内存库运行 `Base.metadata.create_all()`。
- Repository 使用短 `BEGIN IMMEDIATE` 写事务、context-local 原子事务和持久化幂等租约。
- 当前 migration head 为工作树中的 `0006_conversations`。
- 文献元数据适合沿用 SQLite，但应作为独立 literature repository mixin/service，而不是塞入 ChatAgent。
- 原始 PDF 应写入 `allowed_work_root/literature/{paper_id}/source.pdf`；数据库/API 保存可移植相对路径，不回传任意主机路径。

## 6. 文件上传与 PDF 能力

- `python-multipart` 已安装，但当前没有通用文件上传端点。
- 当前没有 PDF parser、OCR、版面分析或结构化论文解析。
- Phase 1 推荐只增加 `pypdf`：依赖小、纯 Python、可逐页抽取文本和基础 document metadata。
- 扫描型 PDF/OCR 不在本阶段范围。无文本层时应返回明确错误，不能把空文档当成功。

## 7. 当前 retrieval / context / memory 状态

- `retrieval/`、`context/`、`memory/` 已明确预留职责，但只有 README 和空 `__init__.py`。
- 这些现有顶层模块应继续使用，不另建第二套 `rag2`/`chat_memory`。
- Phase 1 只实现窄接口与最小无状态实现：Retriever、Reranker、EmbeddingProvider、VectorStore、RagService、ContextManager、MemoryService、CitationService。Embedding 与 VectorStore 不提供生产实现。

## 8. 可复用模块

| 能力 | 复用位置 | 使用方式 |
| --- | --- | --- |
| Session/Conversation | application contracts + SQLite conversation repository | 继续把 conversation 作为 Chat session，不新建重复 session 表 |
| LLM abstraction | agent provider/router/gateway | 通过 adapter 实现 Chat 的 `LLMClient` |
| 配置 | `Settings` | 增加 PDF 大小、chunk target/overlap 等集中配置 |
| 错误 envelope | `ApplicationError` + FastAPI handlers | Literature/Chat 错误映射为稳定 code/status/details |
| 幂等 | application `_idempotent*` + repository leases | 文献摄取使用现有幂等机制 |
| 持久化 | SQLAlchemy/Alembic/SQLite repository | 增加 paper/section/chunk 表和 migration 0007 |
| 上传基础 | FastAPI + python-multipart | 新增受限 PDF multipart endpoint |
| trace/log | API trace middleware + logging | 文献摄取记录 paper id、页数、section/chunk 数，不记录正文 |
| streaming 扩展 | Provider capability + 现有 SSE 模式 | Chat DTO 保留 `stream`，Phase 1 对 `true` 明确返回未支持错误 |

## 9. 当前缺少的模块

- 独立 ChatAgent、IntentRouter、Chat 专用 schema。
- WorkRequest 仅作为未来交接契约的安全预留。
- Paper、PaperSection、Paragraph、PaperChunk、rs-fMRI metadata 模型。
- PDF parser protocol 和逐页 `pypdf` adapter。
- 可替换的 SectionParser 及首个规则实现。
- section/paragraph/sentence-aware Chunker 和 token counter abstraction。
- LiteratureService、repository、迁移与 PDF 上传/查询 API。
- 基于 retrieved chunk 的 CitationService。
- RAG、Memory、Context 的可替换端口。

## 10. Chat Mode 接入点

1. 保留现有 conversation API 作为 session/message 主入口。
2. 将 `ConversationMixin._send_chat_turn` 的 Chat 决策委托给独立 `ChatAgent`，然后仍由现有 repository 原子保存 exchange。
3. `ChatAgent` 只依赖 protocol：IntentRouter、RagService、ContextManager、MemoryService、LLMClient、CitationService。
4. 当前本地文档检索通过 adapter 接到 RagService；下一阶段 Literature Retriever 可替换/组合该 adapter，不修改 ChatAgent。
5. Literature 摄取使用独立 `/api/v1/literature/papers` 路由，不让 ChatAgent 解析 PDF 或写数据库。

## 11. 风险

- 工作树已有大量未提交改动，尤其 conversation/API/前端：必须小范围 patch，不能回退或格式化整文件。
- `contracts.py`、`routes.py` 和总 `RepositoryPort` 已是热点。新增 literature 应尽量使用独立模型、service 和窄 repository protocol，仅在组合根/路由做最少接线。
- PDF 文本顺序取决于文件内部 content stream；多栏、表格、页眉页脚可能乱序。Phase 1 必须把这是启发式解析写入 metadata/warnings。
- 规则 section detector 对排版异常或非英文标题可能退化为 `Unknown/Front Matter`，但不能丢页码。
- 无专用 tokenizer 时 token count 是确定性近似值；接口需允许下一阶段注入模型 tokenizer。
- 上传文件可能损坏、过大、伪装扩展名或无文本层，必须在持久化前失败关闭。
- SQLite 存储全文会增大数据库。本阶段保存可检索 section/chunk 正文，同时保留原 PDF；后续大规模库可迁移索引而不改变模型。

## 12. 推荐目录

遵循现有顶层架构，不机械增加新的 `api/agent/services` 树：

```text
neuroagent/
├─ chat/
│  ├─ agent.py
│  ├─ interfaces.py
│  ├─ models.py
│  └─ services.py
├─ literature/
│  ├─ models.py
│  ├─ ports.py
│  ├─ pdf_parser.py
│  ├─ section_parser.py
│  ├─ chunker.py
│  └─ service.py
├─ retrieval/interfaces.py
├─ context/interfaces.py
└─ memory/interfaces.py
```

SQLite row/migration/repository adapter继续位于 `infrastructure/persistence`；HTTP DTO/route 保持在现有 application/api 层，避免一次性目录重构。

## 13. 推荐依赖

- 新增：`pypdf>=6,<7`，仅用于逐页文本和基础 metadata 抽取。
- 不新增：OCR、PyMuPDF、pdfplumber、LangChain/LlamaIndex、tiktoken、embedding SDK、FAISS/Chroma/Qdrant/Milvus/Elasticsearch。
- token count 先使用可替换的正则近似计数器；未来在 LLM profile 明确后注入对应 tokenizer。

## 14. 本阶段实际修改范围

- 新增 ChatAgent 及 Chat/RAG/Context/Memory/Citation 接口，接入现有 Chat conversation 分支。
- 新增 Literature 模型、PDF 解析、规则 section parser、scientific chunker、持久化和 API。
- 新增集中配置、Alembic 0007、必要的 OpenAPI/API/架构文档和 CHANGELOG 说明。
- 新增 Chat API、session、Paper schema、PDF parser、section parser、chunker、traceability、异常 PDF 测试。
- 不实现/不修改 Work Mode 科研执行、DPABI/DPARSF、MATLAB、ML、SHAP、GraphRAG、embedding、vector database、真实 retrieval/rerank。

## 15. 实现前兼容性结论

设计与当前代码结构兼容：它复用 conversation 作为 session、复用模型网关和 SQLite/Alembic，只给 Chat 分支增加可替换编排层，并把论文摄取放在独立 capability 中。不会改变现有 `/api/v1` URL、工作流状态、科学计划哈希或 Work 执行授权边界。
