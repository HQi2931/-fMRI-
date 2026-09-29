"""SSE transport for the existing durable conversation operation."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import Request
from fastapi.responses import StreamingResponse

from neuroagent.agent.streaming import AnswerStream, observe_answers
from neuroagent.application.contracts import ConversationTurnCreate
from neuroagent.application.errors import ApplicationError
from neuroagent.application.services import NeuroAgentService

logger = logging.getLogger(__name__)
_running: set[asyncio.Task[None]] = set()


def conversation_stream_response(
    service: NeuroAgentService,
    conversation_id: str,
    body: ConversationTurnCreate,
    key: str,
    request: Request,
) -> StreamingResponse:
    # Resolve missing sessions before committing SSE headers, preserving HTTP 404.
    mode = service.get_conversation(conversation_id).mode.value
    trace_id = getattr(request.state, "trace_id", None)

    async def events() -> AsyncIterator[str]:
        queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue(maxsize=64)
        connected = True

        async def emit(event: str, data: dict[str, object]) -> None:
            if connected:
                await queue.put((event, data))

        async def run() -> None:
            try:
                with observe_answers(AnswerStream(mode, emit)):
                    result = await service.send_conversation_turn(conversation_id, body, key)
                await emit("final", result.model_dump(mode="json"))
            except ApplicationError as exc:
                await emit(
                    "error",
                    {
                        "status": exc.status_code,
                        "error": {
                            "code": exc.code,
                            "message": exc.message,
                            "details": exc.details,
                            "trace_id": trace_id,
                        },
                    },
                )
            except Exception:
                logger.exception("Conversation stream operation failed")
                await emit(
                    "error",
                    {
                        "status": 500,
                        "error": {
                            "code": "internal_server_error",
                            "message": "对话处理失败，请重试。",
                            "details": {},
                            "trace_id": trace_id,
                        },
                    },
                )

        task = asyncio.create_task(run())
        _running.add(task)
        task.add_done_callback(_running.discard)
        try:
            yield _frame("status", {"message": "正在准备回答…"})
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _frame(event, data)
                if event in {"final", "error"}:
                    break
        finally:
            # A disconnected reader releases backpressure, while the existing
            # supervised idempotent operation still saves its billable result.
            connected = False
            while not queue.empty():
                queue.get_nowait()

    return StreamingResponse(
        events(),
        status_code=201,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


def _frame(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
