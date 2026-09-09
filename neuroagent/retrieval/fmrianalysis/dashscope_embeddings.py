# ruff: noqa
# mypy: ignore-errors
# Ported from fMRIAnalysis/rsfmri_agent/rag; see ../README.md for provenance.
"""DashScope embedding wrapper — LangChain-compatible, zero model download.

Uses DashScope native API (not OpenAI-compatible mode) for reliability.
Endpoint: POST https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import requests
from langchain_core.embeddings import Embeddings


# ── Configuration ──────────────────────────────────────────────────────────────
DASHSCOPE_EMBED_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
)
MAX_BATCH_SIZE = 10  # DashScope v4 limit per call (v2 allows 25)
MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds


class DashScopeEmbeddings(Embeddings):
    """LangChain-compatible embeddings via DashScope text-embedding API.

    Usage:
        emb = DashScopeEmbeddings(
            api_key=os.environ["DASHSCOPE_API_KEY"],
            model="text-embedding-v2",
        )
        vectors = emb.embed_documents(["text1", "text2"])
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "text-embedding-v4",
        batch_size: int = MAX_BATCH_SIZE,
    ):
        self.api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self.api_key:
            raise ValueError("DASHSCOPE_API_KEY is required")
        self.model = model
        self.batch_size = min(batch_size, MAX_BATCH_SIZE)

    @property
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of documents, batching to API limits."""
        if not texts:
            return []

        all_embeddings = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            batch_vecs = self._embed_batch_with_retry(batch)
            all_embeddings.extend(batch_vecs)

            # Progress indicator for large batches
            if len(texts) > self.batch_size:
                done = min(i + self.batch_size, len(texts))
                print(f"\r  embedding {done}/{len(texts)}", end="", flush=True)

        if len(texts) > self.batch_size:
            print()  # newline after progress
        return all_embeddings

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query text."""
        return self.embed_documents([text])[0]

    def _embed_batch_with_retry(self, texts: list[str]) -> list[list[float]]:
        """Call DashScope API with retry logic."""
        last_error = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(
                    DASHSCOPE_EMBED_URL,
                    headers=self._headers,
                    json={
                        "model": self.model,
                        "input": {"texts": texts},
                        "parameters": {"text_type": "document"},
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                body = resp.json()

                # DashScope response format:
                # {"output": {"embeddings": [{"embedding": [...], "text_index": 0}, ...]}}
                if body.get("code") or body.get("output") is None:
                    error_msg = body.get("message", body.get("code", str(body)))
                    raise RuntimeError(f"DashScope API error: {error_msg}")

                embeddings_list = body["output"]["embeddings"]
                # Sort by text_index to maintain order
                embeddings_list.sort(key=lambda x: x["text_index"])
                return [e["embedding"] for e in embeddings_list]

            except requests.exceptions.HTTPError as exc:
                last_error = exc
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_DELAY * (2**attempt)
                    time.sleep(wait)
            except Exception as exc:
                last_error = exc
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_DELAY * (2**attempt)
                    time.sleep(wait)

        raise RuntimeError(
            f"DashScope embeddings failed after {MAX_RETRIES} attempts"
        ) from last_error
