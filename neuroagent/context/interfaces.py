"""Context assembly contracts reserved for bounded Chat context."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from neuroagent.retrieval.interfaces import RetrievedChunk


class ContextMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str = Field(pattern=r"^(system|user|assistant)$")
    content: str = Field(min_length=1)
    message_id: str | None = None
    sequence: int | None = Field(default=None, ge=1)


class PinnedContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=4_000)
    priority: str = Field(default="high", pattern=r"^(low|medium|high)$")
    source_message_id: str | None = None


class ContextPacket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1)
    recent_messages: tuple[ContextMessage, ...] = ()
    retrieval_context: tuple[RetrievedChunk, ...] = ()
    pinned_context: tuple[PinnedContext, ...] = ()
    conversation_summary: str | None = None
    work_context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextManager(Protocol):
    def build(
        self,
        *,
        question: str,
        recent_messages: tuple[ContextMessage, ...],
        retrieval_context: tuple[RetrievedChunk, ...],
        pinned_context: tuple[PinnedContext, ...],
        conversation_summary: str | None,
        context_window_tokens: int = 16_384,
        max_output_tokens: int = 2_048,
        context_kind: str = "knowledge_query",
        work_context: dict[str, Any] | None = None,
    ) -> ContextPacket: ...
