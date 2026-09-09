"""Memory contracts; phase one only exposes an in-request recent window."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from neuroagent.context.interfaces import ContextMessage, PinnedContext


class MemorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recent_messages: tuple[ContextMessage, ...] = ()
    conversation_summary: str | None = None
    pinned_context: tuple[PinnedContext, ...] = ()


class MemoryService(Protocol):
    def recall(
        self,
        *,
        session_id: str,
        recent_messages: tuple[ContextMessage, ...],
        pinned_context: tuple[PinnedContext, ...],
        conversation_summary: str | None,
    ) -> MemorySnapshot: ...
