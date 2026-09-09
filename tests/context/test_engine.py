"""Context engine contract tests."""

from neuroagent.context.engine import ContextEngine
from neuroagent.context.interfaces import ContextMessage, PinnedContext
from neuroagent.retrieval.interfaces import RetrievedChunk


def test_context_engine_keeps_recent_exchanges_and_respects_budget() -> None:
    engine = ContextEngine()
    messages = tuple(
        ContextMessage(role="user" if index % 2 == 0 else "assistant", content="内容 " * 80)
        for index in range(30)
    )
    packet = engine.build(
        question="ALFF 和 fALFF 有什么区别?",
        recent_messages=messages,
        retrieval_context=tuple(
            RetrievedChunk(
                chunk_id=str(index),
                source="paper",
                title="Methods",
                text="evidence " * 100,
                score=1,
            )
            for index in range(8)
        ),
        pinned_context=(PinnedContext(key="format", value="回答保持简洁"),),
        conversation_summary="此前讨论了低频振幅指标。",
        context_window_tokens=4_096,
        max_output_tokens=512,
        context_kind="knowledge_query",
    )

    assert packet.recent_messages[-1] == messages[-1]
    assert len(packet.recent_messages) % 2 == 0
    assert packet.metadata["estimated_tokens"] <= packet.metadata["token_budget"]
    assert packet.pinned_context[0].key == "format"


def test_context_engine_triggers_summary_for_long_history() -> None:
    engine = ContextEngine()
    messages = tuple(
        ContextMessage(role="user", content=f"问题 {index}", sequence=index + 1)
        for index in range(24)
    )

    assert engine.should_summarize(
        messages,
        covered_sequence=0,
        context_window_tokens=16_384,
        max_output_tokens=2_048,
    )

