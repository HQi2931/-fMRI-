# ruff: noqa
# mypy: ignore-errors
# Ported from fMRIAnalysis/rsfmri_agent/rag; see ../README.md for provenance.
"""Literature retriever for injecting cited passages into LLM prompts.

Uses the ChromaDB index built by chunk_indexer.py to retrieve relevant original
passages from parsed papers, preventing LLM "attribution hallucination" where
the LLM fabricates literature conclusions.

Usage:
    from neuroagent.retrieval.fmrianalysis.retriever import LiteratureRetriever
    retriever = LiteratureRetriever()
    passages = retriever.retrieve("GRF cluster-forming threshold", top_k=5)
    # Returns formatted string with citations for injection into LLM prompt
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from langchain_chroma import Chroma



_log = None

def _logger() -> logging.Logger:
    global _log
    if _log is None:
        _log = logging.getLogger(__name__)
    return _log

# ── Configuration ──────────────────────────────────────────────────────────────
DEFAULT_DB_DIR = "docs/chroma_literature"
DEFAULT_COLLECTION = "fmri_literature_v1"
DEFAULT_TOP_K = 5
SIMILARITY_THRESHOLD = 0.0    # text-embedding-v4 produces low cosine scores (0.0–0.3);
                               # we rely on ranking + rerank rather than hard cutoff
# NOTE: SIMILARITY_THRESHOLD_STRICT removed — SIMILARITY_THRESHOLD=0.0 + min_results
# mechanism already provides adequate quality control. See MIN_CANDIDATES below.

# ── Per-category query expansion strings ─────────────────────────────────────
# Used by both retrieve_for_decision() and search() — single source of truth.

_DECISION_TYPE_QUERIES: dict[str, str] = {
    "correction": (
        "multiple comparison correction method GRF FDR FWE choice for "
        "fMRI group analysis cluster forming threshold recommendation"
    ),
    "smoothing": (
        "fMRI group analysis smoothing kernel FWHM selection spatial "
        "resolution MNI recommendation"
    ),
    "covariate": (
        "fMRI group analysis covariate selection age sex head motion "
        "site demographic nuisance variables"
    ),
    "qc_threshold": (
        "fMRI quality control head motion FD threshold framewise "
        "displacement exclusion criteria tSNR DVARS"
    ),
    "mediation": (
        "mediation analysis Baron Kenny bootstrap indirect effect "
        "brain behavior association fMRI"
    ),
    "classification": (
        "fMRI classification cross validation feature selection "
        "double dipping circular analysis small sample"
    ),
}
MIN_CANDIDATES = 5                  # floor: always keep at least this many from vector stage

# ── Query Expansion — CN→EN term mapping for cross-language retrieval ────────
# text-embedding-v4 is multilingual but Chinese query → English documents can
# produce low similarity. These mappings generate English-only variants when
# Chinese terms are detected, boosting cross-language recall.

CN_EN_TERM_MAP: dict[str, list[str]] = {
    # Multiple comparison correction
    "校正": ["correction", "multiple comparison", "FWE", "FDR", "family-wise error"],
    "多重比较": ["multiple comparison correction", "GRF", "FDR", "FWE", "Bonferroni"],
    "grf": ["Gaussian random field", "cluster-extent thresholding", "voxel-level"],
    "fdr": ["false discovery rate", "Benjamini Hochberg", "q-value"],
    "fwe": ["family-wise error rate", "Bonferroni correction"],
    # Smoothing
    "平滑": ["smoothing", "spatial filter", "Gaussian kernel", "FWHM"],
    "fwhm": ["full width half maximum", "smoothing kernel", "spatial scale"],
    # Head motion
    "头动": ["head motion", "framewise displacement", "FD", "scrubbing", "DVARS"],
    "scrubbing": ["scrubbing", "censoring volumes", "spike regression", "motion artifact"],
    "fd": ["framewise displacement", "head motion threshold", "movement parameter"],
    # Mediation / brain-behavior
    "中介": ["mediation analysis", "indirect effect", "Baron and Kenny", "bootstrap", "Sobel test"],
    "调节": ["moderation analysis", "interaction effect", "simple slope"],
    "相关": ["correlation", "Pearson", "Spearman", "partial correlation", "association"],
    "回归": ["regression", "GLM", "predictor", "covariate"],
    # Classification
    "分类": ["classification", "SVM", "logistic regression", "support vector machine", "ROC AUC"],
    "诊断": ["diagnostic classification", "biomarker", "sensitivity specificity"],
    "交叉验证": ["cross validation", "leave-one-out", "k-fold", "nested CV", "LOOCV"],
    "过拟合": ["overfitting", "regularization", "small sample size", "curse of dimensionality"],
    "特征选择": ["feature selection", "recursive feature elimination", "LASSO", "elastic net"],
    # Network / connectivity
    "功能连接": ["functional connectivity", "FC", "seed-based", "resting-state network", "correlation matrix"],
    "网络": ["brain network", "connectome", "graph theory", "network topology"],
    "图论": ["graph theory", "small-world", "network topology", "modularity", "global efficiency", "clustering coefficient"],
    "动态": ["dynamic functional connectivity", "sliding window", "time-varying", "temporal variability"],
    "种子点": ["seed region", "ROI definition", "seed-based FC", "region of interest"],
    # Preprocessing
    "预处理": ["preprocessing", "pipeline", "nuisance regression", "normalization"],
    "alff": ["amplitude of low frequency fluctuation", "ALFF", "fALFF", "fractional ALFF"],
    "reho": ["regional homogeneity", "ReHo", "Kendall coefficient concordance", "local synchronization"],
    # Analysis
    "样本量": ["sample size", "statistical power", "effect size", "Cohen d"],
    "效应量": ["effect size", "Cohen d", "Hedges g", "Cohen f2"],
    "可重复": ["reproducibility", "replication", "reliability", "generalizability"],
    "假阳性": ["false positive", "type I error", "false discovery", "inflated significance"],
    # General
    "fmri": ["functional MRI", "resting-state fMRI", "BOLD signal", "neuroimaging"],
    "静息态": ["resting-state", "rs-fMRI", "intrinsic brain activity", "spontaneous fluctuation"],
    "组分析": ["group analysis", "second-level", "random effects", "mixed effects", "GLM"],
}

# These terms signal that the query is primarily Chinese → generate EN-only variant


def _detect_chinese(text: str) -> bool:
    """Check if text contains Chinese characters."""
    return bool(re.search(r'[一-鿿]', text))


def _generate_query_variants(query: str) -> list[str]:
    """Generate 2-3 query variants for multi-query retrieval.

    Strategy:
      1. Original query (as-is)
      2. CN→EN expansion: replace Chinese terms with English equivalents
      3. Broadened variant: remove specific numbers/thresholds, keep concepts

    Returns list of 2-3 query strings (deduplicated).
    """
    variants = [query]

    # Variant 2: Pure-EN expansion (only if Chinese detected)
    # Build a clean English-only query from detected domain terms + any
    # English words already in the original query. This avoids the noise
    # of mixing Chinese characters with English document embeddings.
    if _detect_chinese(query):
        # Extract English words already in the query
        en_words = re.findall(r'[a-zA-Z]+(?:\s+[a-zA-Z]+){0,5}', query)
        existing_en = ' '.join(en_words).strip()

        # Collect domain-specific English terms from CN keywords
        domain_terms: list[str] = []
        query_lower = query.lower()
        for cn_key, en_list in CN_EN_TERM_MAP.items():
            if cn_key in query or cn_key in query_lower:
                domain_terms.extend(en_list[:2])  # top 2 most specific terms

        # Build pure-EN variant
        en_parts = []
        if existing_en:
            en_parts.append(existing_en)
        if domain_terms:
            en_parts.append(' '.join(dict.fromkeys(domain_terms)))
        if en_parts:
            en_variant = ' '.join(en_parts)
            if en_variant != query and len(en_variant) > 10:
                variants.append(en_variant)

    # Variant 3: Broadened variant — only for Chinese queries where specific
    # numeric thresholds can create sparsity. English queries already have good
    # domain-specific notation that shouldn't be stripped.
    if _detect_chinese(query):
        # Remove standalone numbers only, not fMRI notation (p<0.001, 6mm, etc.)
        broad = re.sub(
            r'(?<![a-zA-Z<>=/])\b\d+(?:\.\d+)?\b(?!\s*(?:mm|hz|s|ms|%|[a-zA-Z]))',
            '', query,
        )
        broad = re.sub(r'\s{2,}', ' ', broad).strip()
        # Only keep if it meaningfully changes the query
        if broad != query and len(broad) > 20:
            # Don't add if the result has fragments like lone ".2" or trailing "vs"
            if not re.search(r'(?<!\d)\.\d+', broad):
                variants.append(broad)

    # Remove duplicates while preserving order (non-Chinese queries often
    # produce identical variants since only the original is generated).
    seen: set[str] = set()
    unique = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique if unique else [query]


def _reciprocal_rank_fusion(
    results_per_variant: list[list[dict]],
    k_values: list[int] | None = None,
    top_n: int = 5,
) -> list[dict]:
    """Fuse multiple ranked result lists using weighted Reciprocal Rank Fusion.

    RRF score = Σ 1/(k_i + rank) across all result lists, where k_i is
    the constant for variant i. Lower k → higher weight.

    Default: first variant (original query) gets k=30 (higher weight),
    subsequent variants get k=60 (standard weight).

    Args:
        results_per_variant: List of ranked result lists (one per query variant).
        k_values: Per-variant RRF constants. Default: [30, 60, 60, ...].
        top_n: Number of top results to return.

    Returns:
        Fused ranked list of dicts with added ``rrf_score`` field.
    """
    if k_values is None:
        # First variant (original query) gets higher weight
        k_values = [30] + [60] * (len(results_per_variant) - 1)

    scores: dict[str, float] = {}
    docs: dict[str, dict] = {}

    for vi, variant_results in enumerate(results_per_variant):
        k = k_values[vi] if vi < len(k_values) else 60
        seen = set()
        for rank, doc in enumerate(variant_results, 1):
            # Preserve distinct chunks under the same heading.
            key = doc.get("chunk_id") or (doc.get("source", ""), doc.get("content", ""))
            if key in seen:
                continue
            seen.add(key)
            rrf = 1.0 / (k + rank)

            scores[key] = scores.get(key, 0.0) + rrf
            docs.setdefault(key, dict(doc))
            docs[key]["rrf_score"] = scores[key]

    fused = sorted(docs.values(), key=lambda d: d["rrf_score"], reverse=True)
    return fused[:top_n]



class LiteratureRetriever:
    """Retrieve relevant literature passages from the ChromaDB index.

    Singleton-friendly: loads the index once, caches the vectorstore.
    """

    def __init__(
        self,
        db_dir: str | Path = DEFAULT_DB_DIR,
        collection_name: str = DEFAULT_COLLECTION,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
        api_key: str | None = None,
    ):
        self._api_key = api_key
        self._db_dir = Path(db_dir)
        self._collection_name = collection_name
        self._similarity_threshold = similarity_threshold
        self._vectorstore: Optional[Chroma] = None
        self._embeddings: Any = None

    @property
    def is_available(self) -> bool:
        """Check if the ChromaDB index exists."""
        return self._db_dir.exists() and (self._db_dir / "chroma.sqlite3").exists()

    def _ensure_loaded(self):
        """Lazy-load the vectorstore."""
        if self._vectorstore is not None:
            return
        if not self.is_available:
            raise FileNotFoundError(
                f"ChromaDB index not found at {self._db_dir}. "
                "Configure a copy of the existing fMRIAnalysis index."
            )
        from langchain_chroma import Chroma
        from chromadb.config import Settings
        from neuroagent.retrieval.fmrianalysis.dashscope_embeddings import DashScopeEmbeddings
        self._embeddings = DashScopeEmbeddings(api_key=self._api_key)
        self._vectorstore = Chroma(
            collection_name=self._collection_name,
            persist_directory=str(self._db_dir),
            embedding_function=self._embeddings,
            client_settings=Settings(anonymized_telemetry=False),
            create_collection_if_not_exists=False,
        )

    def retrieve(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        filter_category: Optional[str] = None,
        include_scores: bool = False,
        use_rerank: bool = True,
        min_results: int = MIN_CANDIDATES,
    ) -> list[dict]:
        """Retrieve relevant literature passages (two-stage: vector → rerank).

        Stage 1: ChromaDB vector search → top-k*4 candidates
        Stage 2: DashScope GTE-Rerank → top-k final results

        If fewer than ``min_results`` candidates survive the similarity threshold,
        the threshold is relaxed to guarantee at least ``min_results`` candidates
        (taking the top-N by raw vector score regardless of threshold). This
        prevents text-embedding-v4's naturally low cosine scores from killing
        otherwise correctly-ranked results.

        Args:
            query: Search query.
            top_k: Number of passages to return.
            filter_category: Optional category filter.
            include_scores: Include similarity + rerank scores.
            use_rerank: Enable Cross-Encoder rerank (recommended).
            min_results: Minimum candidates to guarantee before rerank stage.

        Returns:
            List of dicts with keys: content, source, category, title, h1, h2, score(s).
        """
        self._ensure_loaded()

        # ── Stage 1: Vector retrieval (high recall) ──
        search_filter = None
        if filter_category:
            search_filter = {"category": filter_category}

        n_candidates = top_k * 4 if use_rerank else top_k * 2
        results = self._vectorstore.similarity_search_with_score(
            query, k=n_candidates, filter=search_filter,
        )

        # Collect ALL vector results with scores (before threshold filtering)
        all_vector_results = []
        for doc, score in results:
            similarity = 1.0 - float(score)
            all_vector_results.append({
                **doc.metadata,
                "chunk_id": doc.metadata.get("chunk_id") or doc.id,
                "content": doc.page_content.strip(),
                "source": doc.metadata.get("source", ""),
                "category": doc.metadata.get("category", ""),
                "title": doc.metadata.get("title", ""),
                "h1": doc.metadata.get("h1", ""),
                "h2": doc.metadata.get("h2", ""),
                "vector_score": round(similarity, 4),
            })

        # ── Threshold filter with min_results safeguard ──
        threshold = self._similarity_threshold
        candidates = [r for r in all_vector_results if r["vector_score"] >= threshold]

        # Relax threshold if too few candidates pass — take top-N by score
        if len(candidates) < min_results and all_vector_results:
            # Sort all results by score descending, take top min_results
            all_vector_results.sort(key=lambda x: x["vector_score"], reverse=True)
            relaxed = all_vector_results[:min_results]
            # Only add candidates that aren't already in the list (avoid duplicates)
            existing_contents = {c["content"][:80] for c in candidates}
            for r in relaxed:
                if r["content"][:80] not in existing_contents:
                    candidates.append(r)
                    existing_contents.add(r["content"][:80])

        if not candidates:
            return []

        # ── Stage 2: Cross-Encoder Rerank (high precision) ──
        if use_rerank and len(candidates) > 1:
            try:
                from neuroagent.retrieval.fmrianalysis.reranker import DashScopeReranker
                reranker = DashScopeReranker(api_key=self._api_key)
                candidates = reranker.rerank_with_texts(
                    query, candidates, text_key="content", top_n=top_k,
                )
            except Exception as exc:
                # Graceful degradation: fall back to vector-only ordering
                _logger().warning("Rerank unavailable (%s), using vector scores", type(exc).__name__)
                candidates = candidates[:top_k]
        else:
            candidates = candidates[:top_k]

        # ── Clean output ──
        output = []
        for c in candidates:
            entry = {
                **c,
                "content": c["content"],
                "source": c.get("source", ""),
                "category": c.get("category", ""),
                "title": c.get("title", ""),
                "h1": c.get("h1", ""),
                "h2": c.get("h2", ""),
            }
            if include_scores:
                entry["vector_score"] = c.get("vector_score")
                entry["rerank_score"] = c.get("rerank_score")
            output.append(entry)

        return output

    def retrieve_formatted(
        self,
        query: str,
        top_k: int = DEFAULT_TOP_K,
        filter_category: Optional[str] = None,
    ) -> str:
        """Retrieve and format passages for injection into an LLM prompt.

        Uses zero similarity threshold (0.0) with min_results floor — relies on
        ranking + rerank for quality rather than hard cutoff, since
        text-embedding-v4 produces low cosine scores (0.0–0.3).
        For Agentic RAG, prefer ``search()`` which adds multi-query + RRF fusion.
        """
        passages = self.retrieve(query, top_k=top_k, filter_category=filter_category)

        if not passages:
            return "[未检索到相关文献原文段落]"

        lines = []
        lines.append("## 📚 从原始论文中检索到的相关原文段落（必须基于这些原文做决策）")
        lines.append("")

        for i, p in enumerate(passages, 1):
            cat = p["category"]
            h1 = p.get("h1", "")
            h2 = p.get("h2", "")
            header = f"### [{i}] {cat}"
            if h1:
                header += f" / {h1}"
            if h2:
                header += f" > {h2}"
            lines.append(header)

            # Truncate very long passages
            content = p["content"]
            if len(content) > 1500:
                content = content[:1500] + "..."
            lines.append(content)
            lines.append("")

        lines.append("---")
        lines.append(
            "⚠️ 以上段落从原始论文中检索，请**严格基于这些原文内容**做出判断。"
            "不要引用未在上文中出现的文献结论。如需引用，请标注具体论文和段落。"
        )
        return "\n".join(lines)

    def retrieve_for_decision(
        self,
        decision_type: str,
        context: dict | None = None,
    ) -> str:
        """Retrieve passages optimized for a specific parameter decision type.

        Uses moderate threshold (0.08) via retrieve_formatted to filter noise,
        with min_results safeguard against empty returns.

        Args:
            decision_type: One of 'correction', 'smoothing', 'covariate',
                           'qc_threshold', 'mediation', 'classification'.
            context: Optional context dict (e.g. {'n_subjects': 228, 'metric': 'alff'}).

        Returns:
            Formatted string ready for LLM prompt injection.
        """
        query = _DECISION_TYPE_QUERIES.get(decision_type, decision_type)

        # Add context to query
        if context:
            if "n_subjects" in context:
                query += f" sample size {context['n_subjects']}"
            if "metric" in context:
                query += f" {context['metric']}"

        return self.retrieve_formatted(query, top_k=5)

    def search(
        self,
        query: str,
        decision_type: str | None = None,
        top_k: int = DEFAULT_TOP_K,
    ) -> dict:
        """Tool-callable search returning structured dict for Agentic RAG.

        Multi-query retrieval with RRF fusion:
          1. decision_type → append domain keywords (if provided)
          2. Generate 2-3 query variants (CN→EN expansion, broadened)
          3. Search each variant independently (vector → rerank)
          4. RRF-fuse results → dedup + re-rank → top_k

        Uses permissive threshold (0.0) because the LLM self-judges relevance.

        Args:
            query: Natural language search query from the LLM.
            decision_type: Optional decision category for query enhancement
                (correction/smoothing/covariate/qc_threshold/mediation/classification).
            top_k: Number of passages to return.

        Returns:
            {"status": "ok"|"empty"|"error",
             "n_results": int,
             "passages": [...],
             "query_used": str,
             "query_variants": [str, ...]}
        """
        # ── Step 1: Base enhancement via decision_type ──
        enhanced_query = query
        if decision_type and decision_type in _DECISION_TYPE_QUERIES:
            enhanced_query = f"{query} {_DECISION_TYPE_QUERIES[decision_type]}"

        # ── Step 2: Generate query variants ──
        variants = _generate_query_variants(enhanced_query)

        # ── Step 3: Search each variant ──
        results_per_variant: list[list[dict]] = []
        for variant in variants:
            try:
                passages = self.retrieve(
                    variant,
                    top_k=top_k * 2,       # more candidates per variant for fusion
                    include_scores=False,
                    use_rerank=True,
                    min_results=top_k,
                )
                results_per_variant.append(passages)
            except Exception:
                results_per_variant.append([])

        # ── Step 4: RRF fusion ──
        if len(results_per_variant) == 1:
            fused = results_per_variant[0]
        else:
            fused = _reciprocal_rank_fusion(
                results_per_variant,
                top_n=top_k,
            )

        if not fused:
            return {
                "status": "empty",
                "n_results": 0,
                "passages": [],
                "query_used": enhanced_query,
                "query_variants": variants,
                "message": "No relevant literature found. Try broader terms or a different angle.",
            }

        # ── Truncate long passages for compact tool results ──
        compact = []
        for p in fused[:top_k]:
            content = p["content"]
            if len(content) > 800:
                content = content[:800] + "..."
            compact.append({
                **p,
                "content": content,
                "source": p.get("source", ""),
                "category": p.get("category", ""),
                "title": p.get("title", ""),
                "h1": p.get("h1", ""),
                "h2": p.get("h2", ""),
            })

        return {
            "status": "ok",
            "n_results": len(compact),
            "passages": compact,
            "query_used": enhanced_query,
            "query_variants": variants,
        }

    def search_formatted_for_tool(self, search_result: dict) -> str:
        """Format a search() result dict as compact text for tool response messages.

        Produces a concise markdown string suitable for injection as a tool
        result in the LLM conversation.
        """
        if search_result.get("status") == "error":
            return (
                f"[文献检索失败] {search_result.get('error', 'Unknown error')}\n"
                f"请基于现有知识继续决策，或尝试调整查询词。"
            )

        if search_result.get("status") == "empty":
            return (
                f"[未检索到相关文献] query='{search_result.get('query_used', '')}'\n"
                f"{search_result.get('message', 'No results.')}\n"
                f"建议：尝试更广泛的查询词，或基于通用方法论知识继续。"
            )

        passages = search_result.get("passages", [])
        lines = [
            f"## 📚 文献检索结果 ({search_result['n_results']} 条)",
            f"查询: {search_result.get('query_used', '')}",
            "",
        ]
        for i, p in enumerate(passages, 1):
            cat = p.get("category", "")
            title = p.get("title", "")
            h1 = p.get("h1", "")
            h2 = p.get("h2", "")
            header = f"### [{i}] {cat}"
            if h1:
                header += f" / {h1}"
            if h2:
                header += f" > {h2}"
            lines.append(header)
            lines.append(p["content"])
            lines.append("")

        lines.append("---")
        lines.append(
            "⚠️ 请**严格基于以上原文段落**做出参数决策。"
            "如果检索结果不足以支撑决策，可以调整查询词再次检索。"
        )
        return "\n".join(lines)


# ── Module-level convenience ───────────────────────────────────────────────────

import threading

_retriever: Optional[LiteratureRetriever] = None
_retriever_lock = threading.Lock()


def get_retriever() -> LiteratureRetriever:
    """Get or create the singleton LiteratureRetriever (thread-safe)."""
    global _retriever
    if _retriever is None:
        with _retriever_lock:
            if _retriever is None:  # double-checked locking
                _retriever = LiteratureRetriever()
    return _retriever


def retrieve_literature(query: str, **kwargs) -> str:
    """Shorthand: retrieve and format literature passages.

    Usage in main.py:
        from neuroagent.retrieval.fmrianalysis.retriever import retrieve_literature
        lit_context = retrieve_literature("GRF cluster-forming threshold")
    """
    return get_retriever().retrieve_formatted(query, **kwargs)


def search_literature(
    query: str,
    decision_type: str | None = None,
    top_k: int = 5,
) -> dict:
    """Shorthand: tool-callable literature search returning structured dict.

    Usage:
        from neuroagent.retrieval.fmrianalysis.retriever import search_literature
        result = search_literature("GRF threshold p<0.001", decision_type="correction")
    """
    return get_retriever().search(query, decision_type=decision_type, top_k=top_k)


def format_search_result(search_result: dict) -> str:
    """Shorthand: format a search() result dict for LLM tool response."""
    return get_retriever().search_formatted_for_tool(search_result)
