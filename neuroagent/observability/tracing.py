"""Lightweight request/worker trace context."""

from __future__ import annotations

from contextvars import ContextVar, Token
from uuid import uuid4

_TRACE_ID: ContextVar[str | None] = ContextVar("neuroagent_trace_id", default=None)


def current_trace_id() -> str:
    return _TRACE_ID.get() or str(uuid4())


def bind_trace_id(trace_id: str) -> Token[str | None]:
    return _TRACE_ID.set(trace_id)


def reset_trace_id(token: Token[str | None]) -> None:
    _TRACE_ID.reset(token)
