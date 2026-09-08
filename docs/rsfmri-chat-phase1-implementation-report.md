# rs-fMRI Chat Mode Phase 1 实施报告

- 日期：2026-09-07
- 范围：Milestone 1～3
- 结果：Chat Skeleton、Literature Management、PDF 结构化解析、section-aware chunking 和 chunk traceability 已完成。按用户后续要求，额外直接移植并优化 fMRIAnalysis 的 RAG；未开发 Work Mode。

## 最新范围调整：复用 fMRIAnalysis RAG

源目录实际为 `D:/Project_for_agent/fMRIAnalysis`。复用其 `rsfmri_agent/rag/retriever.py`、
`dashscope_embeddings.py`、`reranker.py`，新增 `retrieval/fmrianalysis_service.py` 接入现有 RagService。
没有移植其工作流、Agent 运行时、MinerU 云上传或整套依赖。

### 意图路由更新

Chat 意图现在先调用现有模型网关，要求返回 `knowledge_query`、`conversation` 或 `work_request`
三选一 JSON；`work_request` 只接受模型给出的任务名和参数并重新经过 schema 校验。模型不可用、
超时、结构异常或返回未知意图时，自动使用原有规则路由。规则路由仍是安全兜底，且不会自行执行 Work。
因此“你知道怎么做 ALFF 的预处理吗？”会被识别为知识问答；“帮我用数据执行 ALFF 计算”才会生成草案。

优化包括：RRF 跨查询累加；按 chunk ID 去重而非 section 去重；只做一次最终 rerank；保留来源字段；
关闭 Chroma 遥测；依赖延迟加载；外发查询复用脱敏策略；错误不泄露原始响应；不使用不可靠的固定距离阈值。
旧索引默认距离并非可直接解释的 cosine similarity，因此只使用其排序，不把分数当科学可信度。

本机索引副本位于 `D:/rs/-fMRI-/work/rag/fmrianalysis`，包含 2,521 个 1,024 维片段。
离线使用库内已有向量查询：返回 3 个结果并命中原记录，无 embedding 重建、无网络调用。
原索引未改动；副本属于忽略的本地运行产物，不进 Git。

启用方式：`uv sync --extra rag`；在本地 `.env` 设置
`RSFMRI_RAG_DB_DIR=D:/rs/-fMRI-/work/rag/fmrianalysis` 和 `DASHSCOPE_API_KEY`，保持原脱敏配置。
未配置索引时保留原本地文档检索行为。两个项目的 `.env` 虽未发现该 Key，但进一步通过现有 SecretResolver
确认进程环境已提供该 Key，因此本机 `.env` 已启用上述索引副本（未复制或展示密钥）。重启 API 后生效。
未进行需另行授权的真实 Provider smoke；不要把 Key 写入文档或 Git。

额外新增文件：`retrieval/fmrianalysis/{__init__,retriever,dashscope_embeddings,reranker}.py`、
`retrieval/fmrianalysis_service.py`、`tests/backend/test_fmrianalysis_rag.py`、
`infrastructure/migrations/versions/0008_section_order.py`。后者兼容开发期间已初始化的文献库。
额外修改 Settings、服务组合、Chat metadata、可选依赖和锁文件。RAG extra 会带入 Chroma/LangChain
传递依赖，但基础安装不要求它们；未引入第二个向量数据库。迁移来源按用户授权内部复用，外部分发前仍需核对源代码权属。

## 1. 阅读了哪些关键目录和文件

完整盘点了仓库文件树和 Git 状态，并重点阅读：

- 根约束与入口：`AGENTS.md`、`README.md`、`pyproject.toml`、`.env.example`。
- 架构：`docs/architecture/project-structure.md`、`system-design.md`、`mvp-1.0-target-architecture.md`，以及 ADR 0008/0009。
- API：`neuroagent/api/app.py`、`routes.py`、`main.py`、`docs/api/api-v1.md`。
- Agent/LLM：`neuroagent/agent/models.py`、`gateway.py`、`providers.py`、`router.py`、`redaction.py`、`config.py`。
- Application：`contracts.py`、`ports.py`、`services.py`、`settings.py`、`errors.py`、`service_mixins/_base.py`、`conversations.py`、`models.py`。
- Persistence：`database.py`、`models.py`、`repository.py`、conversation/idempotency repository mixins、Alembic 0001～0006。
- 既有扩展位：`retrieval/README.md`、`context/README.md`、`memory/README.md`。
- 现有本地证据：`analysis/rag.py`、`analysis/models.py`。
- 测试：backend/agent/science/integration 的 fixture、API、迁移、架构边界和 Agent gateway 测试。

## 2. 当前项目原来的 Agent / Chat 架构是什么

项目原有多 Provider Agent 栈由 ModelProfile、ModelRouter、ModelProvider、ModelGateway 和
OutboundContextPolicy 组成。当前工作树已经有 SQLite Conversation/Message/ToolCall、Chat/Work mode
API 和前端，但 Chat 的 scope gate、本地文档关键词检索、LLM 调用、citation 拼装和持久化编排主要
集中在 `ConversationMixin`。`retrieval/context/memory` 当时只有 README 占位；当前本地 `analysis/rag.py`
不是论文 RAG。

## 3. 新增了哪些文件

### 文档

- `docs/rsfmri-chat-module-analysis.md`
- `docs/rsfmri-chat-module-design.md`
- `docs/rsfmri-chat-phase1-implementation-report.md`

### Chat / RAG / Context / Memory

- `neuroagent/chat/__init__.py`
- `neuroagent/chat/models.py`
- `neuroagent/chat/interfaces.py`
- `neuroagent/chat/services.py`
- `neuroagent/chat/agent.py`
- `neuroagent/retrieval/interfaces.py`
- `neuroagent/context/interfaces.py`
- `neuroagent/memory/interfaces.py`

### Literature

- `neuroagent/literature/__init__.py`
- `neuroagent/literature/models.py`
- `neuroagent/literature/ports.py`
- `neuroagent/literature/pdf_parser.py`
- `neuroagent/literature/section_parser.py`
- `neuroagent/literature/chunker.py`
- `neuroagent/literature/service.py`
- `neuroagent/infrastructure/persistence/repository_mixins/literature.py`
- `neuroagent/infrastructure/migrations/versions/0007_literature.py`

### 测试

- `tests/backend/test_chat_mode.py`
- `tests/backend/test_literature_api.py`
- `tests/literature/__init__.py`
- `tests/literature/pdf_factory.py`
- `tests/literature/test_models.py`
- `tests/literature/test_pdf_parser.py`
- `tests/literature/test_section_parser.py`
- `tests/literature/test_chunker.py`

## 4. 修改了哪些文件

Phase 1 小范围接线或文档同步涉及：

- `pyproject.toml`、`uv.lock`：增加并锁定 `pypdf`。
- `.env.example`、`application/settings.py`：PDF 上限和 chunk 参数。
- `application/services.py`：组合 ChatAgent 和 LiteratureService。
- `application/contracts.py`：Chat turn 增加 `stream` 扩展字段。
- `application/service_mixins/conversations.py`：仅把 Chat 分支委托给 ChatAgent，保留原 session 持久化。
- `api/routes.py`：Literature upload/get/list API。
- `infrastructure/persistence/models.py`、`repository.py`、repository mixin 导出：Paper/Section/Chunk SQLite adapter。
- `tests/backend/test_migrations.py`：验证新表。
- `tests/backend/conftest.py`、`test_api.py` 的 Settings fixture：显式隔离本机付费 RAG 配置，防止测试误发网络调用。
- `web/openapi.json`、`web/src/api/schema.generated.ts`：机械同步 API schema。
- `README.md`、`CHANGELOG.md`、`docs/api/api-v1.md`、`docs/architecture/project-structure.md` 及 retrieval/context/memory README。

工作树在本任务开始前已有大量未提交的 conversation、Work、前端和 MATLAB 改动；本实现保留了这些
用户改动，没有回退、提交或重写无关文件。

## 5. 为什么这样设计

优先复用已有模块化单体、SQLite、conversation、Provider gateway、错误 envelope 和配置系统；把新增
能力放到独立模块，用窄 protocol 隔离 Chat 编排、PDF、存储和未来 RAG。这样不会引入第二套 session，
不会让 ChatAgent 直接解析文件或访问数据库，也不改变已有 Work/审批/Worker 安全边界。

## 6. ChatAgent 的职责是什么

- 接收已校验的单次 Chat 请求。
- 通过 IntentRouter 区分 knowledge/conversation/work request。
- 读取 MemorySnapshot、构建 ContextPacket。
- 调用 RagService 和可用 LLMClient。
- 调用 CitationService 由类型化 evidence 生成 citation。
- 返回 answer、intent、citation、metadata 或 WorkRequest draft。

它不保存消息、不解析 PDF、不切块、不生成 embedding、不访问 Vector DB、不执行 Work Mode。

## 7. Literature 模块的职责是什么

验证 PDF 摄取边界，协调逐页解析、section 识别、scientific chunking，保存受控原 PDF，并通过独立
repository 保存 Paper/Section/Chunk。它不回答用户问题、不调用 LLM、不建立向量索引。

## 8. PDF 是如何解析的

使用轻量 `pypdf` adapter 从内存字节逐物理页 `extract_text()`，同时读取可空 Title/Author/
原始 metadata，并在前三页文本中启发式查找 DOI。CreationDate 仅保留原始值，不冒充出版年份。原 PDF 计算 SHA-256 后保存于 work root 下的固定
相对位置。扩展名、PDF signature、大小、加密、损坏、空页和无文本层都有显式处理。

## 9. Section 是如何识别的

`RuleBasedSectionParser` 先识别编号层级，再规范化短标题并查询可注入 alias table。支持 Abstract、
Introduction/Background、Methods/Materials and Methods、Participants/Subjects、MRI/Image Acquisition、
Preprocessing、Feature Extraction、Statistical Analysis、Results、Discussion、Conclusion、References。
Methods 子节保存 parent section + subsection。未命中规则的文本保留为 Front Matter。接口可替换为
未来 LLM-based parser。

## 10. Chunk 是如何生成的

`ScientificChunker` 按 `Section -> Paragraph -> Sentence -> Chunk` 处理，仅在同一 section/subsection
内聚合。默认 target 600、overlap 100，由 Settings 配置。优先保留完整句子；只有单句本身超限时才
使用 token window fallback。Phase 1 使用可替换的 Unicode regex 近似 token counter。

## 11. Chunk 如何映射回论文页码

PdfParser 首先给每个 `PageText` 分配 1-based 物理页码。SectionParser 生成的每个 ParagraphSpan 保存
page_start/page_end。Chunker 从实际纳入句子的 ParagraphSpan 取最小/最大页码，同时保存 paper_id、
section、subsection、chunk_index 和 source_section_id。SQLite 分列持久化这些字段；API 读取测试验证
它们在 round trip 后不变。

## 12. 当前有没有实现 Embedding

有可选实现：直接复用 DashScope text-embedding-v4，用于查询既有 1,024 维索引。没有重新嵌入文献，也未自动为新上传论文建索引。

## 13. 当前有没有实现 Vector DB

复用一个已有 Chroma 索引；保留可替换 VectorStore protocol。没有接第二种数据库。

## 14. 当前有没有实现真正的 RAG

已有可选的真实 RAG 代码链路：查询扩展 → DashScope embedding → Chroma → RRF → 可选 rerank → 现有 LLM → 来源列表。
在线端到端尚未实测（项目要求单独授权真实 Provider smoke）；旧索引离线查询已验证。新上传论文的 chunks 尚未自动加入该索引。

## 15. 哪些部分只是为后续 RAG 预留

EmbeddingProvider、VectorStore、Retriever、Reranker、RagService、RetrievedChunk、ContextPacket、
MemorySnapshot、CitationService，以及 chunk 上的 rs-fMRI metadata 字段均是扩展边界。
RAG 已有 fMRIAnalysis 适配实现；新论文 indexing、BM25、Hybrid、严格逐句 citation 校验及长期 memory 尚未实现。

## 16. 如何扩展成 Hybrid Retrieval

实现一个 BM25 Retriever 和一个 Dense Retriever；对同一 query 并行取候选，按 RRF/加权融合并执行
metadata filter，再交给 Reranker。组合后的实现满足现有 RagService protocol 即可，ChatAgent 不变。

## 17. 如何扩展 Citation

下一阶段检索结果直接携带持久化 chunk_id/paper_id/section/page。给模型的每个 evidence 分配稳定编号，
模型只能引用该编号；CitationService 用本次 retrieved set 校验编号并从数据库生成最终引用。不存在于
retrieved set 的来源拒绝或丢弃，禁止模型自由生成 paper/page。

## 18. 如何接 Memory / Context

在现有 MemoryService 后增加 conversation summary 和 pinned-context repository；ContextManager 依预算
按 system、pinned、recent summary、retrieval、recent turns 选择与压缩。当前 conversation 消息已经
持久化并传入 recent-window adapter，但尚未做长期记忆、摘要或 pinned 自动抽取。

## 19. 如何把 Chat 的 work_request 交给未来 Work Mode

当前 IntentRouter 只生成 `status=draft`、`requires_user_confirmation=true` 的 WorkRequest，并明确
`work_dispatched=false`。下一阶段新增 `WorkRequestDispatcher`/Work Orchestrator 端口，在用户确认后把
draft 转换为受控 SkillRequest/Plan；不得从 ChatAgent 直接创建 Run 或执行 MATLAB。

## 20. 测试结果是什么

- 实现前基线：470 collected，467 passed，3 skipped，coverage 85.63%。
- 新增定向测试：37 passed。
- 实现后全量：492 collected，489 passed，3 skipped，coverage 85.98%。
- 跳过项为既有真实 MATLAB/环境型 smoke，不属于本阶段。
- 上述全量结果属于用户调整复用方向前的版本；本次 RAG 调整后不重复大规模测试。
- RAG 调整后定向验证：27 passed（Chat、上传、解析、切块、迁移以及 3 个 RAG 回归测试）。
- 真实索引离线查询：2,521 个片段可读取，已有向量自身检索命中。
- 接口最终同步后再验证 Chat/上传：8 passed；ruff 通过，mypy strict 通过（141 个源文件）。
- OpenAPI 和 TypeScript schema 已重新生成。开发期间本地文献表已升级到带 section_index 的迁移。

## 21. 当前还有哪些已知问题

- `pypdf` 不做 OCR；扫描型 PDF 返回 `pdf_text_layer_missing`。
- 多栏排版、复杂表格、页眉页脚可能造成文本阅读顺序噪声。
- Section alias 以常见英文科研标题为主，非标准/其他语言标题可能退化为 Front Matter。
- token count 是正则近似值，不等同于具体模型 tokenizer。
- 上传论文 chunks 尚未接入检索、rerank 或 citation answer pipeline。
- 旧索引没有 paper_id/page 字段；保留其真实 chunk ID、标题和 section，页码明确为空，不伪造 Citation。
- 尚未验证真实 DashScope 调用质量/延迟；沿用源实现的请求重试，服务故障时可能等待较长。
- 当前来源列表受 retrieved set 约束，但尚未对 LLM 正文每个引用或科学论断进行逐句校验。
- 当前 context 只完成结构组包，现有 ModelGateway adapter 尚未消费 summary/pinned/recent messages。
- multipart 上传尚未使用现有 JSON 幂等租约；不确定响应时需先核对 SHA-256/列表，重复上传会形成新 Paper。
- 列表接口当前返回完整 sections/chunks，论文库很大后应增加分页和 summary DTO。

## 22. 下一阶段最推荐做什么

先授权一次轻量在线检索（本机已接入索引副本，环境中已有 Key）；随后把新上传的带页码 chunks 增量写入独立 Chroma collection，
不要重建旧库。优先完成新文献 Citation 闭环，再按实际检索质量决定是否添加 BM25 + RRF Hybrid。
仅选少量真实科研问题验证来源与答案质量，不堆叠低价值测试。

## Phase 1 边界确认

本阶段没有新增或修改 DPABI、DPARSF、MATLAB workflow、统计执行、机器学习、ROC、SHAP、GraphRAG、
知识图谱或 multi-agent 功能。Chat 中识别到执行需求时只产生 draft，不触发 Work Mode。
