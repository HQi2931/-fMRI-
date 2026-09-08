# ruff: noqa
# mypy: ignore-errors
# Ported from fMRIAnalysis/rsfmri_agent/rag; see ../README.md for provenance.
"""DashScope Rerank API — second-stage precision booster for RAG retrieval.

Takes top-N candidate passages from ChromaDB vector search and re-ranks them
with a Cross-Encoder model via DashScope, producing high-precision ordering.

API: POST https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
Model: gte-rerank (GTE-based Cross-Encoder, CN+EN mixed)

Usage:
    from neuroagent.retrieval.fmrianalysis.reranker import DashScopeReranker
    reranker = DashScopeReranker()
    ranked = reranker.rerank("query", ["doc1", "doc2", ...], top_n=5)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

_log = None


def _logger() -> logging.Logger:
    global _log
    if _log is None:
        _log = logging.getLogger(__name__)
    return _log


# ── Configuration ──────────────────────────────────────────────────────────────
RERANK_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
RERANK_MODEL = "qwen3-rerank"
MAX_DOCS_PER_CALL = 50  # DashScope rerank limit
MAX_RETRIES = 3
RETRY_DELAY = 1.0


class DashScopeReranker:
    """Cross-Encoder reranker via DashScope GTE-Rerank API.

    Reranks candidate documents given a query, returning top-N with scores.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = RERANK_MODEL,
    ):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY is required")
        self.model = model

    @property
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int = 5,
        return_documents: bool = False,
    ) -> list[dict]:
        """Rerank documents by relevance to query.

        Args:
            query: Search query.
            documents: Candidate document texts (max 50).
            top_n: Number of top results to return.
            return_documents: Whether to include full document text in output.

        Returns:
            List of dicts sorted by relevance::
                [{"index": int, "relevance_score": float, "document": str (optional)}]
        """
        if not documents:
            return []
        if len(documents) > MAX_DOCS_PER_CALL:
            documents = documents[:MAX_DOCS_PER_CALL]

        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    RERANK_URL,
                    headers=self._headers,
                    json={
                        "model": self.model,
                        "input": {
                            "query": query,
                            "documents": documents,
                        },
                        "parameters": {
                            "top_n": min(top_n, len(documents)),
                            "return_documents": return_documents,
                        },
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                body = resp.json()

                if body.get("code") or body.get("output") is None:
                    error_msg = body.get("message", body.get("code", str(body)))
                    raise RuntimeError(f"DashScope Rerank error: {error_msg}")

                results = body["output"].get("results", [])
                # DashScope returns results sorted by relevance_score descending.
                # Do NOT re-sort by index — that destroys the relevance ordering.
                return results

            except requests.exceptions.HTTPError as exc:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_DELAY * (2**attempt)
                    _logger().warning("Rerank HTTP retry type=%s", type(exc).__name__)
                    time.sleep(wait)
                    continue
                raise
            except Exception as exc:
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_DELAY * (2**attempt)
                    _logger().warning("Rerank retry type=%s", type(exc).__name__)
                    time.sleep(wait)
                    continue
                raise

        return []  # unreachable

    def rerank_with_texts(
        self,
        query: str,
        document_dicts: list[dict],
        text_key: str = "content",
        top_n: int = 5,
    ) -> list[dict]:
        """Rerank structured documents, merging rerank scores back.

        Args:
            query: Search query.
            document_dicts: List of dicts, each with a text field under ``text_key``.
            text_key: Key in each dict containing the document text.
            top_n: Number of top results to return.

        Returns:
            document_dicts reordered by rerank score, with ``rerank_score`` added.
        """
        if not document_dicts:
            return []

        texts = [d[text_key] for d in document_dicts]
        ranked = self.rerank(query, texts, top_n=top_n)

        output = []
        for r in ranked:
            idx = r["index"]
            if type(idx) is int and 0 <= idx < len(document_dicts):
                doc = dict(document_dicts[idx])
                doc["rerank_score"] = round(r["relevance_score"], 4)
                output.append(doc)
        return output
