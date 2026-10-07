"""Request-local, transport-independent previews of visible model answers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

StreamSink = Callable[[str, dict[str, object]], Awaitable[None]]


class AnswerStream:
    def __init__(self, mode: str, emit: StreamSink) -> None:
        self.mode = mode
        self.emit = emit

    async def reset(self) -> None:
        await self.emit("reset", {})
        if self.mode == "work":
            await self.status("正在整理方案与回答…")

    async def status(self, message: str) -> None:
        if self.mode == "work":
            await self.emit("status", {"message": message})

    async def text(self, text: str) -> None:
        if not text:
            return
        if self.mode == "work":
            # Work answers are rewritten after validation; only final carries
            # the authoritative prose. Keep native provider streaming internal.
            return
        await self.emit("delta", {"text": text})


_current: ContextVar[AnswerStream | None] = ContextVar("conversation_answer_stream", default=None)


def current_answer_stream() -> AnswerStream | None:
    return _current.get()


@contextmanager
def observe_answers(observer: AnswerStream) -> Iterator[None]:
    token = _current.set(observer)
    try:
        yield
    finally:
        _current.reset(token)
