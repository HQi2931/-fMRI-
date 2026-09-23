"""Chat multi-hop retrieval through the bounded tool loop.

Two levels are covered: the agent-level flow with deterministic retrieval (to
pin citation identity), and the real service wiring that decides whether a
tool-capable profile activates the loop at all.
"""

# ruff: noqa: RUF001

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from neuroagent.agent.gateway import ChatMessages, ToolTurnResult
from neuroagent.agent.models import (
    ModelCapability,
    ModelProfile,
    ProviderResponse,
    ToolCall,
)
from neuroagent.agent.providers import MockProvider
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.api.app import create_app
from neuroagent.application.chat_tools import build_chat_dispatcher
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service
from neuroagent.chat.agent import ChatAgent
from neuroagent.chat.evidence import EvidenceCollector
from neuroagent.chat.models import ChatAgentRequest
from neuroagent.chat.services import (
    DEFAULT_CITATION_SERVICE,
    DEFAULT_CONTEXT_MANAGER,
    DEFAULT_MEMORY_SERVICE,
    RuleBasedIntentRouter,
)
from neuroagent.chat.tool_loop import ChatToolRunner
from neuroagent.retrieval.interfaces import RetrievalResult, RetrievedChunk

_SALT = "stable-chat-tool-loop-redaction-salt"


class _StubLlmClient:
    """Single-shot client; records whether the fallback path was taken."""

    def __init__(self) -> None:
        self.generate_calls = 0

    def available(self, preferred_profile_id: str | None) -> bool:
        del preferred_profile_id
        return True

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.generate_calls += 1
        raise AssertionError("the single-shot path must not run when the loop answered")


def _chunk(chunk_id: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, source=f"{chunk_id}.pdf", title=chunk_id, text=text, score=1.0
    )


def _messages() -> ChatMessages:
    return ChatMessages(
        messages=[
            {"role": "system", "content": "你是 rs-fMRI 助手。"},
            {"role": "user", "content": "ALFF 的生理含义是什么?"},
        ],
        payload={},
        context_hash="a" * 64,
        redaction_count=0,
    )


def _runner(
    *,
    retrieved: list[RetrievedChunk],
    turns: list[ToolTurnResult],
    search_seen: list[tuple[str, int]],
) -> ChatToolRunner:
    async def retrieve(query: str, limit: int) -> RetrievalResult:
        search_seen.append((query, limit))
        return RetrievalResult(in_scope=True, chunks=tuple(retrieved))

    async def call_turn(**kwargs: Any) -> ToolTurnResult:
        del kwargs
        if not turns:
            raise AssertionError("provider turn queue exhausted")
        return turns.pop(0)

    async def probe(preferred_profile_id: str | None) -> bool:
        del preferred_profile_id
        return True

    return ChatToolRunner(
        build_messages=lambda **kwargs: _messages(),
        call_turn=call_turn,
        dispatcher_factory=lambda collector: build_chat_dispatcher(
            retrieve=retrieve,
            collector=collector,
            policy=OutboundContextPolicy(_SALT),
        ),
        probe_capability=probe,
    )


def _turn(content: str = "", tool_calls: tuple[ToolCall, ...] = ()) -> ToolTurnResult:
    return ToolTurnResult(
        response=ProviderResponse(content=content, model="tool-model", tool_calls=tool_calls),
        selected_profile_id="tool-profile",
        attempted_profile_ids=("tool-profile",),
    )


async def test_chat_loop_uses_tool_retrieval_for_citations() -> None:
    chunk_a = _chunk("chunk-a", "ALFF reflects the amplitude of low-frequency fluctuations.")
    chunk_b = _chunk("chunk-b", "fALFF normalises ALFF by the whole-band amplitude.")
    search_seen: list[tuple[str, int]] = []
    runner = _runner(
        retrieved=[chunk_a, chunk_b],
        turns=[
            _turn(
                tool_calls=(
                    ToolCall(
                        call_id="call-1",
                        name="search_evidence",
                        arguments={"query": "ALFF physiological meaning", "limit": 5},
                    ),
                )
            ),
            _turn(content="ALFF 反映低频振幅 [C1]，fALFF 是它的归一化形式 [C2]。多余引用 [C9]。"),
        ],
        search_seen=search_seen,
    )
    llm_client = _StubLlmClient()
    agent = ChatAgent(
        intent_router=RuleBasedIntentRouter(),
        rag_service=_StubRag(chunk_a),
        context_manager=DEFAULT_CONTEXT_MANAGER,
        memory_service=DEFAULT_MEMORY_SERVICE,
        llm_client=llm_client,  # type: ignore[arg-type]
        citation_service=DEFAULT_CITATION_SERVICE,
        tool_runner=runner,
    )

    response = await agent.respond(
        ChatAgentRequest(session_id="session-1", message="ALFF 的生理含义是什么?")
    )

    # The loop answered, so the single-shot client was never used.
    assert llm_client.generate_calls == 0
    assert search_seen == [("ALFF physiological meaning", 5)]

    loop_meta = response.metadata["tool_loop"]
    assert loop_meta["enabled"] is True
    assert loop_meta["stop_reason"] == "completed"
    assert loop_meta["steps"] == 2
    assert loop_meta["tool_names"] == ["search_evidence"]

    # Citation identity survives: C1/C2 resolve to real retrieved chunks.
    assert [citation.citation_id for citation in response.citations] == ["C1", "C2"]
    assert {citation.chunk_id for citation in response.citations} == {"chunk-a", "chunk-b"}

    # The existing citation guard still applies to loop-produced answers.
    assert "[C1]" in response.answer
    assert "[C2]" in response.answer
    assert "[C9]" not in response.answer
    assert "引用不完整" in response.answer
    assert response.metadata["retrieved_chunk_count"] == 2


class _StubRag:
    """Upfront retrieval stub: the loop must be able to extend its evidence."""

    def __init__(self, *chunks: RetrievedChunk) -> None:
        self._chunks = chunks

    async def retrieve(
        self,
        query: str,
        *,
        limit: int = 8,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        del query, limit, filters
        return RetrievalResult(in_scope=True, chunks=self._chunks)


async def test_chat_loop_falls_back_when_it_produces_no_answer() -> None:
    """A budget- or provider-limited loop must not degrade the reply."""

    class _FallbackClient:
        def __init__(self) -> None:
            self.generate_calls = 0

        def available(self, preferred_profile_id: str | None) -> bool:
            del preferred_profile_id
            return True

        async def generate(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            self.generate_calls += 1
            from neuroagent.chat.interfaces import LlmResult

            return LlmResult(
                content="单次检索回答。",
                model="single-shot",
                profile_id="plain-profile",
                context_hash="b" * 64,
            )

    search_seen: list[tuple[str, int]] = []
    runner = _runner(
        retrieved=[_chunk("chunk-a", "text")],
        turns=[
            _turn(
                tool_calls=(
                    ToolCall(call_id="c1", name="search_evidence", arguments={"query": "x"}),
                )
            )
        ],
        search_seen=search_seen,
    )
    client = _FallbackClient()
    agent = ChatAgent(
        intent_router=RuleBasedIntentRouter(),
        rag_service=_StubRag(_chunk("chunk-a", "text")),
        context_manager=DEFAULT_CONTEXT_MANAGER,
        memory_service=DEFAULT_MEMORY_SERVICE,
        llm_client=client,  # type: ignore[arg-type]
        citation_service=DEFAULT_CITATION_SERVICE,
        tool_runner=runner,
    )

    response = await agent.respond(
        ChatAgentRequest(session_id="session-2", message="ALFF 的生理含义是什么?")
    )

    # The loop exhausted its scripted turns without a final answer, so the
    # single-shot path supplied the reply.
    assert client.generate_calls == 1
    assert response.answer == "单次检索回答。"
    assert "tool_loop" not in response.metadata


def test_evidence_collector_assigns_stable_citation_ids() -> None:
    collector = EvidenceCollector()
    first = collector.add([_chunk("a", "one"), _chunk("b", "two")])
    assert [citation_id for citation_id, _ in first] == ["C1", "C2"]

    # Re-adding a known chunk must not shift existing identifiers.
    again = collector.add([_chunk("a", "one"), _chunk("c", "three")])
    assert [citation_id for citation_id, _ in again] == ["C1", "C3"]
    assert [chunk.chunk_id for chunk in collector.chunks] == ["a", "b", "c"]


def test_evidence_collector_respects_its_limit() -> None:
    collector = EvidenceCollector(limit=2)
    collector.add([_chunk("a", "1"), _chunk("b", "2"), _chunk("c", "3")])
    assert [chunk.chunk_id for chunk in collector.chunks] == ["a", "b"]


# ---------------------------------------------------------------------------
# Service wiring: a tool-capable profile activates the loop end to end.
# ---------------------------------------------------------------------------


def _tool_profile() -> ModelProfile:
    return ModelProfile(
        id="tool-profile",
        provider="openai-compatible",
        base_url="https://example.org/v1",
        model="tool-model",
        api_key_env="TOOL_LOOP_API_KEY",
        capabilities=frozenset({ModelCapability.JSON_OBJECT, ModelCapability.TOOL_USE}),
    )


def _plain_profile() -> ModelProfile:
    return ModelProfile(
        id="plain-profile",
        provider="openai-compatible",
        base_url="https://example.org/v1",
        model="plain-model",
        api_key_env="TOOL_LOOP_API_KEY",
        capabilities=frozenset({ModelCapability.JSON_OBJECT}),
    )


@pytest.mark.parametrize(
    ("profile_factory", "expect_loop"),
    [(_tool_profile, True), (_plain_profile, False)],
)
def test_service_activates_the_loop_only_for_tool_capable_profiles(
    tmp_path: Path,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_factory: Any,
    expect_loop: bool,
) -> None:
    routing = ProviderResponse(
        content='{"intent":"knowledge_query","query":"ALFF 生理含义"}',
        model="tool-model",
    )
    if expect_loop:
        # Routing, then one tool turn, then the loop's final answer.
        queue: list[Any] = [
            routing,
            ProviderResponse(
                content="",
                model="tool-model",
                tool_calls=(
                    ToolCall(
                        call_id="call-1",
                        name="search_evidence",
                        arguments={"query": "ALFF low frequency amplitude"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ProviderResponse(content="ALFF 反映低频振幅。", model="tool-model"),
        ]
    else:
        # A non-tool profile answers in one shot after routing.
        queue = [routing, ProviderResponse(content="单次回答。", model="tool-model")]

    provider = MockProvider(queue)
    settings = Settings(
        rag_db_dir=None,
        database_url=f"sqlite:///{(tmp_path / 'tool-loop.sqlite').as_posix()}",
        allowed_source_roots=[source_root],
        allowed_work_root=work_root,
        redaction_salt=_SALT,
        secrets_file=tmp_path / ".env",
    )
    monkeypatch.setenv("TOOL_LOOP_API_KEY", "test-secret")
    service = build_service(settings, providers={"openai-compatible": provider})
    profile = profile_factory()
    try:
        service.repository.create_model_profile(profile)
        with TestClient(create_app(service=service)) as client:
            conversation = client.post(
                "/api/v1/conversations",
                json={"mode": "chat", "preferred_profile_id": profile.id},
                headers={"Idempotency-Key": "tool-loop-create"},
            ).json()
            response = client.post(
                f"/api/v1/conversations/{conversation['conversation_id']}/turns",
                json={
                    "content": "ALFF 的生理含义是什么?",
                    "preferred_profile_id": profile.id,
                },
                headers={"Idempotency-Key": "tool-loop-turn"},
            )
            reloaded = client.get(
                f"/api/v1/conversations/{conversation['conversation_id']}",
            ).json()
    finally:
        service.close()

    assert response.status_code == 201, response.text
    payload = response.json()["assistant_message"]["payload"]
    chat_metadata = payload["chat"]["metadata"]

    # The intent-routing call never carries tools.
    assert provider.tool_requests[0] == ()
    if expect_loop:
        # Both tool turns and everything after them carry the whole catalog.
        assert provider.tool_requests[1] == ("list_skills", "search_evidence")
        assert provider.tool_requests[2] == ("list_skills", "search_evidence")
        tool_loop = chat_metadata["tool_loop"]
        assert tool_loop["enabled"] is True
        assert tool_loop["stop_reason"] == "completed"
        assert tool_loop["steps"] == 2
        assert tool_loop["tool_names"] == ["search_evidence"]
        assert payload["chat"]["answer"] == "ALFF 反映低频振幅。"
        assert chat_metadata["llm_used"] is True

        # Every loop step is audited on the conversation, not just in metadata.
        steps = response.json()["tool_calls"]
        assert [step["tool_name"] for step in steps] == ["search_evidence"]
        assert steps[0]["step_index"] == 1
        assert steps[0]["risk_tier"] == "read"
        assert steps[0]["status"] == "succeeded"
        assert steps[0]["duration_ms"] is not None
        assert steps[0]["input"]["query"] == "ALFF low frequency amplitude"

        # The audit rows are durable and ordered: the turn's summary record plus
        # one row per loop step, so a reload shows the same sequence.
        reloaded_steps = reloaded["tool_calls"]
        assert [step["tool_name"] for step in reloaded_steps] == [
            "rsfmri_chat_llm",
            "search_evidence",
        ]
        assert [step["step_index"] for step in reloaded_steps] == [None, 1]
    else:
        # A profile without tool_use must never be offered tools.
        assert all(not entry for entry in provider.tool_requests)
        assert "tool_loop" not in chat_metadata
        assert payload["chat"]["answer"] == "单次回答。"
        assert response.json()["tool_calls"] == []
