from __future__ import annotations

from dataclasses import dataclass

import pytest
import requests

from neuroagent.retrieval.fmrianalysis import dashscope_embeddings, reranker


@dataclass
class FakeResponse:
    body: dict
    error: Exception | None = None

    def raise_for_status(self) -> None:
        if self.error is not None:
            raise self.error

    def json(self) -> dict:
        return self.body


def test_dashscope_embeddings_batch_order_empty_and_query(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []

    def post(_url, **kwargs):
        texts = kwargs["json"]["input"]["texts"]
        calls.append(texts)
        return FakeResponse(
            {
                "output": {
                    "embeddings": [
                        {"text_index": index, "embedding": [float(len(text))]}
                        for index, text in reversed(list(enumerate(texts)))
                    ]
                }
            }
        )

    monkeypatch.setattr(dashscope_embeddings.requests, "post", post)
    client = dashscope_embeddings.DashScopeEmbeddings(api_key="secret", batch_size=2)
    assert client._headers["Authorization"] == "Bearer secret"
    assert client.embed_documents([]) == []
    assert client.embed_documents(["a", "bb", "ccc"]) == [[1.0], [2.0], [3.0]]
    assert calls == [["a", "bb"], ["ccc"]]
    assert "embedding 3/3" in capsys.readouterr().out
    assert client.embed_query("four") == [4.0]


def test_dashscope_embeddings_require_key_and_bound_retries(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="required"):
        dashscope_embeddings.DashScopeEmbeddings()
    monkeypatch.setattr(dashscope_embeddings.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        dashscope_embeddings.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse({}, requests.exceptions.HTTPError("temporary")),
    )
    client = dashscope_embeddings.DashScopeEmbeddings(api_key="secret")
    with pytest.raises(RuntimeError, match="3 attempts") as error:
        client.embed_query("text")
    assert isinstance(error.value.__cause__, requests.exceptions.HTTPError)


def test_dashscope_embeddings_reject_api_error(monkeypatch) -> None:
    monkeypatch.setattr(dashscope_embeddings.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        dashscope_embeddings.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse({"code": "Invalid", "message": "bad"}),
    )
    with pytest.raises(RuntimeError, match="3 attempts"):
        dashscope_embeddings.DashScopeEmbeddings(api_key="secret").embed_query("text")


def test_dashscope_reranker_success_empty_limit_and_structured_merge(monkeypatch) -> None:
    captured: dict = {}

    def post(_url, **kwargs):
        captured.update(kwargs["json"])
        return FakeResponse(
            {
                "output": {
                    "results": [
                        {"index": 1, "relevance_score": 0.98765},
                        {"index": 0, "relevance_score": 0.5},
                        {"index": "bad", "relevance_score": 1},
                    ]
                }
            }
        )

    monkeypatch.setattr(reranker.requests, "post", post)
    client = reranker.DashScopeReranker(api_key="secret")
    assert client._headers["Authorization"] == "Bearer secret"
    assert client.rerank("q", []) == []
    docs = [{"content": "first", "id": 1}, {"content": "second", "id": 2}]
    merged = client.rerank_with_texts("query", docs, top_n=10)
    assert [item["id"] for item in merged] == [2, 1]
    assert merged[0]["rerank_score"] == 0.9877
    assert captured["parameters"]["top_n"] == 2
    assert client.rerank_with_texts("query", []) == []


def test_dashscope_reranker_requires_key_retries_and_surfaces_error(monkeypatch) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="required"):
        reranker.DashScopeReranker()
    monkeypatch.setattr(reranker.time, "sleep", lambda _seconds: None)
    attempts = 0

    def post(_url, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary")
        return FakeResponse({"output": {"results": []}})

    monkeypatch.setattr(reranker.requests, "post", post)
    assert reranker.DashScopeReranker(api_key="secret").rerank("q", ["d"]) == []
    assert attempts == 3

    monkeypatch.setattr(
        reranker.requests,
        "post",
        lambda *_args, **_kwargs: FakeResponse({}, requests.exceptions.HTTPError("rejected")),
    )
    with pytest.raises(requests.exceptions.HTTPError, match="rejected"):
        reranker.DashScopeReranker(api_key="secret").rerank("q", ["d"])
