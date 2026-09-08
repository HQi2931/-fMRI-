"""Narrow dependencies consumed by ChatAgent."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from neuroagent.chat.models import ChatAgentRequest, ChatIntent, Citation, WorkRequest
from neuroagent.context.interfaces import ContextPacket
from neuroagent.retrieval.interfaces import RetrievedChunk


class LlmResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(min_length=1)
    model: str
    profile_id: str
    context_hash: str
    attempted_profile_ids: tuple[str, ...] = ()
    usage: dict[str, int] = Field(default_factory=dict)
    provider_citations: tuple[dict[str, str], ...] = ()
    remote_search_used: bool = False


class IntentRouter(Protocol):
    def route(self, message: str) -> (
        tuple[ChatIntent, WorkRequest | None] | Awaitable[tuple[ChatIntent, WorkRequest | None]]
    ): ...


class LLMClient(Protocol):
    def available(self, preferred_profile_id: str | None) -> bool: ...

    async def generate(
        self,
        context: ContextPacket,
        *,
        preferred_profile_id: str | None,
        model: str | None,
        allow_remote_search: bool,
    ) -> LlmResult: ...


class CitationService(Protocol):
    def build(
        self,
        chunks: tuple[RetrievedChunk, ...],
        *,
        provider_citations: tuple[dict[str, str], ...] = (),
    ) -> tuple[Citation, ...]: ...


class ChatAgentError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ChatAgentPort(Protocol):
    async def respond(self, request: ChatAgentRequest) -> Any: ...
