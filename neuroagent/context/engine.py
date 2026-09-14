"""Bounded context assembly for Chat and Work conversations."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from typing import Any

from neuroagent.context.interfaces import ContextMessage, ContextPacket, PinnedContext
from neuroagent.retrieval.interfaces import RetrievedChunk

_TOKEN = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]")


class ContextBudgetError(ValueError):
    pass


class RegexTokenCounter:
    """Small conservative counter shared by all OpenAI-compatible profiles."""

    def count(self, value: object) -> int:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        total = 0
        for token in _TOKEN.findall(text):
            total += math.ceil(len(token) / 4) if token.isascii() and token.isalnum() else 1
        return max(total, 1)


class ContextEngine:
    def __init__(self, token_counter: RegexTokenCounter | None = None) -> None:
        self._tokens = token_counter or RegexTokenCounter()

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
    ) -> ContextPacket:
        available = int((context_window_tokens - max_output_tokens) * 0.85)
        question_tokens = self._tokens.count(question)
        if question_tokens >= available:
            raise ContextBudgetError("current question exceeds the model context budget")
        content_budget = available - question_tokens
        shares = {
            "knowledge_query": (0.40, 0.30, 0.20, 0.10),
            "conversation": (0.0, 0.55, 0.35, 0.10),
            "work": (0.10, 0.25, 0.20, 0.45),
        }.get(context_kind, (0.40, 0.30, 0.20, 0.10))
        evidence_limit, messages_limit, memory_limit, work_limit = (
            int(content_budget * share) for share in shares
        )

        selected_pins = self._take(pinned_context, memory_limit)
        memory_used = self._sum(selected_pins)
        summary = conversation_summary
        if summary and memory_used + self._tokens.count(summary) > memory_limit:
            summary = None
        messages = self._take_recent_exchanges(recent_messages, messages_limit)
        evidence = self._take(retrieval_context, evidence_limit)
        bounded_work = dict(work_context or {})
        if bounded_work and self._tokens.count(bounded_work) > work_limit:
            bounded_work = {}

        selected_tokens = (
            question_tokens
            + self._sum(selected_pins)
            + self._sum(messages)
            + self._sum(evidence)
            + (self._tokens.count(summary) if summary else 0)
            + (self._tokens.count(bounded_work) if bounded_work else 0)
        )
        metadata = {
            "phase": "context_engine_v1",
            "context_kind": context_kind,
            "token_budget": available,
            "estimated_tokens": selected_tokens,
            "selected": {
                "messages": len(messages),
                "evidence": len(evidence),
                "memories": len(selected_pins),
                "summary": bool(summary),
                "work_state": bool(bounded_work),
            },
            "omitted": {
                "messages": len(recent_messages) - len(messages),
                "evidence": len(retrieval_context) - len(evidence),
                "memories": len(pinned_context) - len(selected_pins),
            },
        }
        return ContextPacket(
            question=question,
            recent_messages=messages,
            retrieval_context=evidence,
            pinned_context=selected_pins,
            conversation_summary=summary,
            work_context=bounded_work,
            metadata=metadata,
        )

    def should_summarize(
        self,
        messages: tuple[ContextMessage, ...],
        *,
        covered_sequence: int,
        context_window_tokens: int,
        max_output_tokens: int,
    ) -> bool:
        pending = tuple(
            item for item in messages if item.sequence is None or item.sequence > covered_sequence
        )
        conversation_budget = int((context_window_tokens - max_output_tokens) * 0.85 * 0.30)
        return len(pending) >= 24 or self._sum(pending) > int(conversation_budget * 0.70)

    def extractive_summary(self, messages: tuple[ContextMessage, ...]) -> str:
        older = messages[:-12] if len(messages) > 12 else messages
        parts = [f"{item.role}: {item.content[:240]}" for item in older[-12:]]
        return "\n".join(parts)

    def _take(self, items: Iterable[Any], limit: int) -> tuple[Any, ...]:
        selected: list[Any] = []
        used = 0
        for item in items:
            size = self._tokens.count(item.model_dump(mode="json"))
            if used + size > limit:
                continue
            selected.append(item)
            used += size
        return tuple(selected)

    def _take_recent_exchanges(
        self, messages: tuple[ContextMessage, ...], limit: int
    ) -> tuple[ContextMessage, ...]:
        pairs: list[tuple[ContextMessage, ...]] = []
        cursor = len(messages)
        while cursor > 0:
            start = max(0, cursor - 2)
            pairs.append(messages[start:cursor])
            cursor = start
        selected: list[tuple[ContextMessage, ...]] = []
        used = 0
        for pair in pairs:
            size = self._sum(pair)
            if used + size > limit:
                break
            selected.append(pair)
            used += size
        return tuple(item for pair in reversed(selected) for item in pair)

    def _sum(self, items: Iterable[Any]) -> int:
        return sum(
            self._tokens.count(
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            )
            for item in items
        )
