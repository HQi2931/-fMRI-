"""Request-local, transport-independent previews of visible model answers."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

StreamSink = Callable[[str, dict[str, object]], Awaitable[None]]


class AnswerStream:
    def __init__(self, mode: str, emit: StreamSink) -> None:
        self.mode = mode
        self.emit = emit
        self._buffer = ""
        self._visible = ""

    async def reset(self) -> None:
        self._buffer = ""
        self._visible = ""
        await self.emit("reset", {})

    async def text(self, text: str) -> None:
        if not text:
            return
        if self.mode == "work":
            self._buffer += text
            visible = _json_answer_prefix(self._buffer)
            delta = visible[len(self._visible) :]
            self._visible = visible
        else:
            delta = text
        if delta:
            await self.emit("delta", {"text": delta})


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


def _json_answer_prefix(source: str) -> str:
    """Decode only a top-level JSON answer, including incomplete string chunks.

    Other values are skipped with the JSON decoder, so nested `answer` fields
    and text inside parameters cannot be mistaken for the user-visible answer.
    Incomplete escapes and surrogate pairs are withheld until they are complete.
    This preview confers no validity or authority on the whole object.
    """
    decoder = json.JSONDecoder()
    position = 0

    def whitespace(index: int) -> int:
        while index < len(source) and source[index].isspace():
            index += 1
        return index

    position = whitespace(position)
    if position >= len(source) or source[position] != "{":
        return ""
    position += 1
    while True:
        position = whitespace(position)
        try:
            key, position = decoder.raw_decode(source, position)
        except ValueError:
            return ""
        if not isinstance(key, str):
            return ""
        position = whitespace(position)
        if position >= len(source) or source[position] != ":":
            return ""
        position = whitespace(position + 1)
        if key == "answer":
            if position >= len(source) or source[position] != '"':
                return ""
            start = position
            position += 1
            while position < len(source):
                character = source[position]
                if character == '"':
                    try:
                        result = decoder.raw_decode(source, start)[0]
                    except ValueError:
                        return ""
                    return result if isinstance(result, str) else ""
                if character == "\\":
                    end = position + 2
                    if end > len(source):
                        break
                    if source[position + 1] == "u":
                        end = position + 6
                        if end > len(source):
                            break
                        try:
                            code = int(source[position + 2 : end], 16)
                        except ValueError:
                            return ""
                        if 0xD800 <= code <= 0xDBFF:
                            if end + 6 > len(source):
                                break
                            if source[end : end + 2] != "\\u":
                                return ""
                            try:
                                low = int(source[end + 2 : end + 6], 16)
                            except ValueError:
                                return ""
                            if not 0xDC00 <= low <= 0xDFFF:
                                return ""
                            end += 6
                        elif 0xDC00 <= code <= 0xDFFF:
                            return ""
                    position = end
                else:
                    position += 1
            try:
                result = json.loads(source[start:position] + '"')
            except ValueError:
                return ""
            return result if isinstance(result, str) else ""
        try:
            _, position = decoder.raw_decode(source, position)
        except ValueError:
            return ""
        position = whitespace(position)
        if position >= len(source) or source[position] != ",":
            return ""
        position += 1
