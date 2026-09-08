# rs-fMRI Chat Mode Phase 1 设计

## 用户后续要求：直接复用已有 RAG

本更新替代下文原设计中“仅 RAG 接口、不引入向量实现”的范围约束，其余模块边界不变。
使用 `FmriAnalysisRagService` 适配 `D:/Project_for_agent/fMRIAnalysis/rsfmri_agent/rag` 的现成代码：
查询脱敏 → 中英扩展 → DashScope text-embedding-v4 → 既有 Chroma → 修正的 RRF → 一次最终 rerank
→ RetrievedChunk → 原 ChatAgent/LLM/Citation。没有增加第二套 Agent 或 conversation。

配置 `RSFMRI_RAG_DB_DIR` 后启用；否则保留原文档关键词检索。Chroma 与 requests 位于可选 rag extra。
原索引只复制使用、不重建 embedding；新 PDF chunks 暂不自动加入。旧索引没有页码则明确为空；
不能把 chunk_index 或 Markdown 行号解释为论文页码。HTTP 失败映射为 503，重排失败降级为 RRF 排序。
索引与模型维度保持兼容，不使用未经校准的固定分数作为证据真实性阈值。

仅做 RRF/去重/来源和适配器关键回归、真实索引离线向量查询，避免扩大测试矩阵。

- 日期：2026-09-07
- 状态：Implementation-ready
- 范围：Milestone 1 Chat Skeleton、Milestone 2 Literature Management、Milestone 3 Scientific Chunking

## 1. 总体架构

```text
User
  -> FastAPI conversation/literature endpoints
     -> Conversation application use case
        -> ChatAgent
           -> IntentRouter
           -> MemoryService (Phase 1: recent messages only / no durable memory engine)
           -> RagService (Phase 1: current local evidence adapter)
           -> ContextManager
           -> LLMClient (adapter over existing ModelGateway)
           -> CitationService (only retrieved evidence)
        -> ConversationRepository (existing SQLite session/messages/tool audit)

PDF upload
  -> LiteratureService
     -> PdfParser
     -> SectionParser
     -> ScientificChunker
     -> LiteratureRepository
        -> SQLite Paper + Section + Chunk
        -> allowed_work_root/literature/{paper_id}/source.pdf
```

ChatAgent 不持有 SQLAlchemy session，不读取 PDF，不切块，不生成 embedding，不访问具体 Vector DB，也不保存 conversation。API 不实现论文结构识别或 chunk 算法。

## 2. 模块职责

### `chat`

- `ChatAgent`：编排单次请求；判断 intent；读取最小 memory/context；调用 RAG/LLM；通过 CitationService 生成引用；返回稳定结果。
- `IntentRouter`：模型优先输出 `knowledge_query`、`conversation` 或 `work_request` 的 allow-listed JSON；模型不可用、超时或输出非法时回退到可解释的规则实现。
- `LLMClient`：Chat 专用窄接口；生产 adapter 复用 `ModelGateway.generate_chat`。
- `CitationService`：只接受 RagService 返回的 evidence；禁止从 LLM 文本反向猜测来源。

### `literature`

- `PdfParser`：验证并逐页抽取文本/基础 metadata；不识别章节。
- `SectionParser`：把有页码的 page text 转换为 section/paragraph；首个实现为规则 + 文本结构检测；接口可替换为 LLM parser。
- `ScientificChunker`：在 section 内按 paragraph/sentence 聚合；超长句才使用 token-window fallback。
- `LiteratureService`：限制上传、协调 parse/section/chunk、保存源文件和持久化结构结果；单篇失败不影响服务进程。
- `LiteratureRepository`：Paper/Section/Chunk 的窄持久化端口。

### `retrieval`

- 定义 `EmbeddingProvider`、`VectorStore`、`Retriever`、`Reranker`、`RagService` 协议和 retrieved chunk 模型。
- 初始范围只预留接口；按上述最新要求增加现有 embedding/Chroma 的可选适配，不重建索引。

### `context` / `memory`

- `ContextManager` 负责把 system、recent messages、retrieval、pinned context 组为有预算的 packet。
- `MemoryService` 负责 recent/summary/pinned 的读取接口。
- Phase 1 只实现无状态/传入式版本，不做 summary、长期记忆或 pinned 自动抽取。

## 3. 数据流

### Chat 请求

1. API 校验 conversation/session 存在、mode 为 chat、消息非空。
2. Conversation use case 取得现有 recent messages，但不把数据库职责交给 ChatAgent。
3. ChatAgent 运行 IntentRouter。
4. `work_request`：生成安全、结构化的 WorkRequest draft；不调用 Work Orchestrator、不执行任何任务。
5. `knowledge_query`：RagService 返回真实 evidence（Phase 1 为当前本地文档 evidence）；ContextManager 组包；若存在可用 Profile，LLMClient 生成回答，否则使用受控的本地回答。
6. CitationService 只由 retrieved evidence 构建 citation。
7. Conversation use case 持久化 user/assistant message、agent metadata 和工具审计。

### PDF 摄取

1. API 读取不超过 `literature_max_pdf_bytes + 1` 字节；拒绝超限、非 `.pdf`、非 PDF signature。
2. LiteratureService 计算 SHA-256、生成 paper id，在持久化前调用 PdfParser。
3. PdfParser 逐页抽取 `PageText(page_number, text)`，读取可空 metadata；损坏/加密/无文本层映射为明确错误。
4. SectionParser 规范化标题别名，生成 `PaperSection` 和逐页 paragraph spans。
5. ScientificChunker 仅在同一 section/subsection 内聚合句子，计算页码范围和 token count。
6. 原 PDF 以固定文件名保存在受控 work root；Paper/Section/Chunk 在一个数据库事务中保存。
7. 返回 Paper、sections、chunks 和 warnings；不创建 embedding/index。

## 4. Chat schema

沿用现有 `ConversationView`、`ConversationMessageView`、`ConversationTurnCreate/View` 作为 HTTP session schema。新增 ChatAgent 内部契约：

```json
{
  "session_id": "conversation UUID",
  "message": "ALFF 是什么？",
  "stream": false,
  "recent_messages": [],
  "pinned_context": []
}
```

```json
{
  "session_id": "conversation UUID",
  "message_id": "由 conversation repository 生成",
  "intent": "knowledge_query",
  "answer": "...",
  "citations": [],
  "work_request": null,
  "metadata": {
    "retrieval_performed": true,
    "llm_used": false,
    "streaming": false
  }
}
```

`stream` 从 Phase 1 起进入 ChatAgent 请求，但当前只接受 `false`。`true` 返回稳定 `chat_streaming_not_implemented`，为以后 SSE/NDJSON token stream 留出兼容空间，避免伪装成 streaming。

## 5. Paper schema

```json
{
  "paper_id": "UUID",
  "title": null,
  "authors": [],
  "year": null,
  "journal": null,
  "doi": null,
  "abstract": null,
  "source_file": "literature/{paper_id}/source.pdf",
  "source_sha256": "64 hex",
  "page_count": 12,
  "metadata": {},
  "created_at": "UTC timestamp"
}
```

所有 bibliographic 字段允许为空或空集合。显式请求 metadata 可覆盖 PDF metadata，但不伪造缺失值；自动抽取来源和 warnings 写入 metadata。

`PaperSection`：

```json
{
  "section_id": "stable UUID/hash",
  "paper_id": "UUID",
  "title": "2.3 Image preprocessing",
  "section": "Methods",
  "subsection": "Preprocessing",
  "level": 2,
  "page_start": 5,
  "page_end": 6,
  "paragraphs": [
    {"text": "...", "page_start": 5, "page_end": 5}
  ],
  "metadata": {}
}
```

## 6. Chunk schema

```json
{
  "chunk_id": "stable UUID/hash",
  "paper_id": "UUID",
  "section": "Methods",
  "subsection": "Preprocessing",
  "page_start": 5,
  "page_end": 6,
  "text": "...",
  "token_count": 526,
  "chunk_index": 7,
  "metadata": {
    "modality": "rs-fMRI",
    "topic": null,
    "software": [],
    "metrics": [],
    "frequency_band": [],
    "atlas": [],
    "dataset": [],
    "harmonization": [],
    "statistics": []
  }
}
```

`metadata` 使用有类型的 `RsFmriMetadata` 作为默认结构，并允许额外的 parser/chunker provenance 放在外层 metadata 扩展区。Phase 1 不做实体识别。

## 7. WorkRequest 预留 schema

```json
{
  "intent": "work_request",
  "task": "calculate_alff",
  "parameters": {
    "frequency_band": [0.01, 0.08]
  },
  "references": [],
  "status": "draft",
  "requires_user_confirmation": true
}
```

Phase 1 的 router 只生成 draft，可识别有限 task label（例如 `calculate_alff`、`preprocess_rsfmri`、`analyze_data`、`unspecified_work`）。它不调用现有 Work conversation action、不创建 Plan/Run、不生成 MATLAB。未来通过 `WorkRequestDispatcher` 端口把已确认 draft 交给 Work Orchestrator。

## 8. Section 识别设计

规则实现先移除编号前缀（如 `2`、`2.1`、罗马数字），再对规范化标题做别名匹配。首批 canonical aliases：

- Abstract
- Introduction / Background
- Methods / Materials and Methods
- Participants / Subjects / Sample
- MRI Acquisition / Image Acquisition / Data Acquisition
- Preprocessing / Image Preprocessing / Data Preprocessing
- Feature Extraction
- Statistical Analysis
- Results
- Discussion
- Conclusion / Conclusions
- References / Bibliography

标题还需满足结构信号（短行、标题大小写/编号、非句末、有限词数），避免把正文中出现的 “methods” 当标题。无法识别时保留为 `Front Matter` 或当前 section，不丢弃文本。规则表是构造参数，未来 LLM parser 实现同一 `SectionParser` protocol。

## 9. Chunk 生成设计

- 配置：默认 target 600 tokens、overlap 100、允许范围 400～800/80～150；集中放在 `Settings`。
- 层次：Section -> Paragraph -> Sentence -> Chunk。
- 正常情况：在同一 section/subsection 内按句子累积到 target；优先在 paragraph 边界结束。
- 超长 paragraph：按句子拆；超长单句：最后才按 tokenizer token window fallback。
- overlap：从上一 chunk 末尾选取不超过配置的完整句子；不得跨 section。
- token counter：Phase 1 的 Unicode regex 近似计数器；失败映射为 `tokenizer_failed`。接口允许下一阶段注入模型 tokenizer。
- `chunk_index` 在 paper 内从 0 连续递增。

## 10. 页码与 traceability

页码不是从 chunk 文本重新推断，而是从最初 `PageText.page_number` 逐层传递：

```text
PageText(page_number)
  -> Paragraph(page_start/page_end)
     -> SentenceSpan(page_start/page_end)
        -> PaperChunk(page_start=min, page_end=max)
```

每个 chunk 同时保存 `paper_id`、section/subsection、page range、source section id（metadata）和 source SHA-256（可由 Paper join 获得）。CitationService 使用持久化 retrieved chunk 构造引用，不允许模型提交自由 `paper_id/page`。

## 11. 错误处理

| 场景 | code | HTTP | 行为 |
| --- | --- | --- | --- |
| 空 Chat | request validation | 422 | Pydantic 拒绝 |
| session 不存在 | not_found | 404 | 沿用 repository |
| stream=true | chat_streaming_not_implemented | 422 | 明确不伪装 streaming |
| LLM timeout/异常 | chat_model_unavailable | 503 | 不保存伪回答；幂等 reservation 可重试 |
| 非 PDF/伪 signature | unsupported_file_type | 415 | 不写文件/数据库 |
| PDF 超大 | pdf_too_large | 413 | 读取上限后中止 |
| PDF 损坏 | pdf_parse_failed | 422 | 单请求失败，服务继续 |
| 加密 PDF | pdf_encrypted | 422 | Phase 1 不接受密码 |
| 无文本层 | pdf_text_layer_missing | 422 | 提示需要 OCR，OCR 不在范围 |
| metadata 缺失 | 无错误 | 201 + warning | 字段为空 |
| section 无法识别 | 无错误 | 201 + warning | 保留 Front Matter/Unknown |
| tokenizer 失败 | tokenizer_failed | 422 | 不保存部分 chunks |
| 持久化失败 | internal_server_error | 500 | 临时文件清理；不暴露路径/正文 |

## 12. RAG 扩展点

Phase 1 只定义：

- `EmbeddingProvider.embed_documents/embed_query`
- `VectorStore.upsert/delete/query`
- `Retriever.retrieve(query, filters, limit)`
- `Reranker.rerank(query, candidates, limit)`
- `RagService.retrieve(query, filters)`

下一阶段可组合：BM25 Retriever + Dense Retriever -> reciprocal-rank fusion -> metadata filter -> cross-encoder/LLM reranker。VectorStore 从 Chroma 换为 Qdrant 时，只替换 adapter 和组合根；ChatAgent、CitationService、PaperChunk 不变。

## 13. Citation 扩展点

Phase 1 citation 模型包含 `citation_id`、`chunk_id`、`paper_id`、title、section/subsection、page_start/page_end、excerpt。下一阶段 LLM prompt 使用受控 evidence label（如 `[C1]`），回答后 CitationService 只解析这些允许 label 并与本次 retrieved set 交叉验证；未知 label 丢弃或使回答失败，绝不接受模型自造来源。

## 14. Memory / Context 扩展点

`MemoryService` 后续可返回：recent window、conversation summary、pinned records；`ContextManager` 按 system -> pinned -> retrieval -> summary -> recent 的优先级和 token budget 组包。Pinned record 使用 `key/value/priority/source_message_id`，压缩时不可静默丢失 high priority。Phase 1 不自动写长期 memory。

## 15. 持久化设计

- `papers`：书目信息、source relative path/hash、page count、metadata/warnings、created_at。
- `paper_sections`：section identity、canonical section/subsection、level、page range、text/paragraph JSON、metadata。
- `literature_chunks`：chunk identity、paper id、section id、section/subsection、page range、text、token count、index、metadata。
- 外键对 Paper 使用 `ON DELETE CASCADE`；Phase 1 不提供删除 API。
- `(paper_id, chunk_index)` 唯一；`paper_id/section/page` 建普通索引，为后续 metadata filter 和 BM25/vector join 准备。

## 16. 测试设计

- ChatAgent/Chat API：基础问答、空请求、session 不存在、stream capability、work_request 只生成 draft。
- Session：create/restore/message sequence 和 Chat/Work mode 边界。
- Paper schema：nullable metadata、rs-fMRI metadata defaults、页码约束。
- PDF parser：内存生成的双页文本 PDF、metadata、损坏、无文本层、unsupported、oversize。
- Section parser：编号、多种同义标题、subsection parent、fallback。
- Chunker：target/overlap、section 边界、连续 index、超长句 fallback。
- Traceability：每个 chunk 的 paper id、section id、page range 能映射到持久化 Paper/Section；跨页 chunk 取正确范围。
- Migration：新增三张表。
- 回归：先运行现有 470 项，再运行新增测试，最后运行全量质量检查中的可用静态检查。

## 17. 明确非目标

不实现 Work Orchestrator、DPABI/DPARSF、MATLAB 执行、统计/ML/SHAP、OCR、Hybrid Retrieval、GraphRAG、知识图谱或 multi-agent。Embedding/Chroma/reranker 仅直接复用现有 RAG，不另造系统。
