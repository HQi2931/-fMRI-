from pathlib import Path
from unittest.mock import Mock

import pytest

from neuroagent.chat.models import ChatIntent
from neuroagent.chat.services import ModelIntentRouter
from neuroagent.retrieval.fmrianalysis.retriever import _reciprocal_rank_fusion
from neuroagent.retrieval.fmrianalysis_service import FmriAnalysisRagService, _to_chunk


def test_fusion_accumulates_and_keeps_distinct_chunks_in_same_section() -> None:
    a = {"chunk_id": "a", "source": "paper", "h1": "Methods", "content": "first"}
    b = {"chunk_id": "b", "source": "paper", "h1": "Methods", "content": "second"}
    result = _reciprocal_rank_fusion([[a, b], [b]], k_values=[60, 60])
    assert [d["chunk_id"] for d in result] == ["b", "a"]
    assert result[0]["rrf_score"] == pytest.approx(1 / 62 + 1 / 61)


def test_legacy_provenance_never_invents_page_or_paper_id() -> None:
    legacy = {"source": "D:/private/paper.md", "content": "Methods text", "h1": "Methods"}
    chunk = _to_chunk(legacy)
    assert chunk.source == "paper.md"
    assert chunk.paper_id is chunk.page_start is chunk.page_end is None
    assert chunk.section == "Methods"
    assert chunk.chunk_id == _to_chunk(legacy).chunk_id
    traced = _to_chunk(
        {**legacy, "chunk_id": "c1", "paper_id": "p1", "page_start": 5, "page_end": 6}
    )
    assert (traced.chunk_id, traced.paper_id, traced.page_start, traced.page_end) == (
        "c1",
        "p1",
        5,
        6,
    )


async def test_adapter_queries_variants_without_per_query_rerank(tmp_path: Path) -> None:
    (tmp_path / "chroma.sqlite3").touch()
    service = FmriAnalysisRagService(
        db_dir=tmp_path,
        collection="test",
        secret_resolver=Mock(resolve=Mock(return_value="test-only")),
        api_key_env="TEST_KEY",
        redaction_salt="test-only-redaction-salt",
        rerank=False,
    )
    retriever = Mock()
    retriever.retrieve.return_value = [
        {"chunk_id": "c1", "content": "ALFF evidence", "source": "paper.md", "h1": "Methods"}
    ]
    service._retriever = retriever
    result = await service.retrieve("ALFF 是什么?")
    assert len(result.chunks) == 1
    assert result.metadata["backend"] == "fmrianalysis"
    assert retriever.retrieve.call_count >= 2
    assert all(not call.kwargs["use_rerank"] for call in retriever.retrieve.call_args_list)


async def test_model_router_parses_allowlisted_json() -> None:
    router = ModelIntentRouter(
        lambda _message: _async_text('{"intent":"knowledge_query"}')
    )
    assert await router.route("ALFF 是什么?") == (ChatIntent.KNOWLEDGE_QUERY, None)


async def test_model_router_falls_back_when_model_output_is_invalid() -> None:
    router = ModelIntentRouter(lambda _message: _async_text("not-json"))
    assert await router.route("你知道怎么做 ALFF 的预处理吗?") == (
        ChatIntent.KNOWLEDGE_QUERY,
        None,
    )


async def _async_text(value: str) -> str:
    return value
