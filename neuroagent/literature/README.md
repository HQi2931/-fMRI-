# Literature 模块

- 状态：Implementing
- 文档类型：Architecture
- 维护者：backend_service_engineer
- 更新日期：2026-09-23
- 关联：[Phase 1 设计](../../docs/development/rsfmri-chat-module-design.md)

`neuroagent.literature` 负责 PDF 文献摄取与可追溯分块，为 Chat 的证据检索提供带页码链路的片段。

当前能力：

- `PypdfParser` 逐页解析 PDF；
- `RuleBasedSectionParser` 识别 section 结构；
- `ScientificChunker` 生成带来源、页码和 section 的 `PaperChunk`；
- 摄入结果经 `PaperIngestResult` 返回，保留可复核的溯源字段。

本模块不做嵌入、不建索引、不调用模型，也不联网。向量检索与 DashScope/Chroma 适配属于 `neuroagent/retrieval`；证据问答编排属于 `neuroagent/chat`。