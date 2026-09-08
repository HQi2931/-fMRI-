# retrieval

## 模块职责

`retrieval` 负责知识文档摄取、解析、分块、索引、关键词搜索、向量搜索、融合检索、重排和证据片段返回。

## 模块边界

检索结果只是证据，不是最终事实，也不直接替 Agent 决策。本模块不部署向量数据库、不读取领域数据格式、不执行领域分析。

## 依赖关系

`retrieval` 被 `context` 调用以提供 EvidenceChunk；可依赖 `infrastructure/vector_store` 和 `infrastructure/persistence` 的适配接口，依赖 `observability` 记录 EvidenceRetrieved。

## 当前阶段

已在 `interfaces.py` 建立 `EmbeddingProvider`、`VectorStore`、`Retriever`、`Reranker`、
`RagService` 和带来源字段的 `RetrievedChunk` 协议。

按用户要求，`fmrianalysis/` 直接移植 `D:/Project_for_agent/fMRIAnalysis/rsfmri_agent/rag/`
的 retriever、DashScopeEmbeddings 和 DashScopeReranker。`fmrianalysis_service.py` 做 Chat 接线，
修复 RRF 累加和 chunk 去重、合并最终重排、保留来源、隔离错误及外发脱敏。移植文件保留原风格，
暂不纳入 strict lint/type 扫描；适配层接受检查。没有复用 Work 或云端 PDF 上传。

安装 `uv sync --extra rag`，设置 `RSFMRI_RAG_DB_DIR` 为已有索引的副本，以及
`DASHSCOPE_API_KEY` 和现有脱敏配置。collection 默认 `fmri_literature_v1`。
未配置旧索引且没有可检索上传论文时继续本地文档检索。不要在原索引上做迁移或重建。

旧索引没有页码时明确返回未知。上传 PDF 后显式调用加入知识库接口，文本经外发脱敏策略后发送 DashScope；以持久化 chunk ID upsert 到工作目录 literature_index 的 uploaded_literature_v1 collection。索引状态持久化为 not_indexed/indexing/ready/failed，只有 ready 论文参与检索。按 paper_ids 检索只查询指定上传论文，否则合并旧库与上传论文候选后执行一次重排。失败保留论文并支持手动重试，不自动补建索引。
RAG extra 的直接依赖是 langchain-chroma、requests；Chroma 及其传递依赖仅可选安装。

## 后续核心接口

- `EmbeddingProvider`
- `VectorStore`
- `Retriever`
- `Reranker`
- `RagService`
