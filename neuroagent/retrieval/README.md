# retrieval

## 模块职责

`retrieval` 负责知识文档摄取、解析、分块、索引、关键词搜索、向量搜索、融合检索、重排和证据片段返回。

## 模块边界

检索结果只是证据，不是最终事实，也不直接替 Agent 决策。本模块不部署向量数据库、不读取领域数据格式、不执行领域分析。

## 依赖关系

`retrieval` 被 `context` 调用以提供 EvidenceChunk；可依赖 `infrastructure/vector_store` 和 `infrastructure/persistence` 的适配接口，依赖 `observability` 记录 EvidenceRetrieved。

## 当前阶段

仅建立 ingestion、chunking、indexing、search 和 reranking 的目录边界，未实现真实 RAG。

## 后续核心接口

- `KnowledgeDocument`
- `EvidenceChunk`
- `RetrievalService`
- `DocumentIngestor`
- `Chunker`
- `SearchBackend`
- `Reranker`
