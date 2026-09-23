"""Chat-side driver for the bounded tool loop.

The runner owns no provider and no tool implementation: it receives the rendered
prompt, a dispatcher factory and a raw provider-turn callable.  That keeps the
loop's safety rules (budgets, tier boundary, untrusted wrapping) in
``neuroagent.agent`` while Chat keeps control of its own prompt and citations.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from neuroagent.agent.gateway import ChatMessages, ToolTurnResult
from neuroagent.agent.loop import (
    UNTRUSTED_TOOL_RESULT_INSTRUCTION,
    AgentLoop,
    AgentLoopBudget,
    ToolInvoker,
)
from neuroagent.agent.models import ProviderResponse
from neuroagent.agent.tool_result import ToolCallOutcome
from neuroagent.agent.tool_spec import LlmTool
from neuroagent.chat.evidence import EvidenceCollector

logger = logging.getLogger(__name__)


class ChatToolLoopResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    final_text: str = ""
    stop_reason: str
    steps: int = Field(default=0, ge=0)
    tool_names: tuple[str, ...] = ()
    outcomes: tuple[ToolCallOutcome, ...] = ()
    usage: dict[str, int] = Field(default_factory=dict)
    context_hash: str = ""
    redaction_count: int = Field(default=0, ge=0)
    model: str | None = None
    profile_id: str | None = None
    attempted_profile_ids: tuple[str, ...] = ()

    @property
    def tool_calls(self) -> int:
        return len(self.tool_names)


BuildMessages = Callable[..., ChatMessages]
CallTurn = Callable[..., Awaitable[ToolTurnResult]]
DispatcherFactory = Callable[[EvidenceCollector], ToolInvoker]
CapabilityProbe = Callable[[str | None], Awaitable[bool]]


class ChatToolRunner:
    def __init__(
        self,
        *,
        build_messages: BuildMessages,
        call_turn: CallTurn,
        dispatcher_factory: DispatcherFactory,
        probe_capability: CapabilityProbe,
        budget: AgentLoopBudget | None = None,
    ) -> None:
        self._build_messages = build_messages
        self._call_turn = call_turn
        self._dispatcher_factory = dispatcher_factory
        self._probe_capability = probe_capability
        self._budget = budget or AgentLoopBudget()

    async def available(self, preferred_profile_id: str | None) -> bool:
        try:
            return await self._probe_capability(preferred_profile_id)
        except Exception:
            # An unavailable probe must degrade to the single-shot path, never
            # break the request.
            logger.warning("Chat tool capability probe failed", exc_info=True)
            return False

    async def run(
        self,
        *,
        question: str,
        evidence: Sequence[Mapping[str, Any]],
        recent_messages: Sequence[Mapping[str, Any]],
        pinned_context: Sequence[Mapping[str, Any]],
        conversation_summary: str | None,
        work_context: Mapping[str, Any],
        preferred_profile_id: str | None,
        model: str | None,
        collector: EvidenceCollector,
        context_window_tokens: int = 16_384,
        max_output_tokens: int = 2_048,
    ) -> ChatToolLoopResult:
        rendered = self._build_messages(
            question=question,
            evidence=[dict(item) for item in evidence],
            recent_messages=[dict(item) for item in recent_messages],
            pinned_context=[dict(item) for item in pinned_context],
            conversation_summary=conversation_summary,
            work_context=dict(work_context),
            allow_web_search=False,
        )
        dispatcher = self._dispatcher_factory(collector)
        tools: tuple[LlmTool, ...] = dispatcher.catalog

        messages: list[dict[str, Any]] = [dict(message) for message in rendered.messages]
        if messages and messages[0].get("role") == "system":
            system_content = str(messages[0].get("content") or "")
            messages[0] = {
                "role": "system",
                "content": f"{system_content}\n{UNTRUSTED_TOOL_RESULT_INSTRUCTION}",
            }

        captured: dict[str, Any] = {}

        async def call_model(
            transcript: list[dict[str, Any]], *, remaining_seconds: float | None = None
        ) -> ProviderResponse:
            turn = await self._call_turn(
                messages=transcript,
                tools=tools,
                preferred_profile_id=preferred_profile_id,
                model=model,
                remaining_seconds=remaining_seconds,
            )
            captured["profile_id"] = turn.selected_profile_id
            captured["attempted"] = turn.attempted_profile_ids
            return turn.response

        budget = replace(
            self._budget,
            context_window_tokens=context_window_tokens,
            max_output_tokens=max_output_tokens,
        )
        result = await AgentLoop(dispatcher, budget=budget).run(
            messages=messages,
            call_model=call_model,
        )
        return ChatToolLoopResult(
            final_text=result.final_text,
            stop_reason=result.stop_reason.value,
            steps=result.steps,
            tool_names=tuple(outcome.name for outcome in result.outcomes),
            outcomes=result.outcomes,
            usage=result.usage,
            context_hash=rendered.context_hash,
            redaction_count=rendered.redaction_count,
            model=result.provider_model,
            profile_id=captured.get("profile_id"),
            attempted_profile_ids=tuple(captured.get("attempted", ())),
        )
