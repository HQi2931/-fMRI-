"""Model execution and budget policy for Work role turns."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.application.work_prompts import work_system_prompt
from neuroagent.application.work_tools import AgentExecutionContext, WorkToolRunner
from neuroagent.context.engine import RegexTokenCounter
from neuroagent.multi_agent.registry import WorkAgentRole

logger = logging.getLogger(__name__)


def _work_loop_metadata(looped: Any) -> dict[str, Any]:
    """Persist loop outcomes even when the final answer requires fallback."""

    workspace_redactions = 0
    workspace_omitted_sensitive = 0
    for outcome in looped.outcomes:
        if outcome.name not in {"read_workspace_content", "search_workspace_content"}:
            continue
        result = outcome.result or {}
        workspace_redactions += int(result.get("redactions", 0) or 0)
        workspace_omitted_sensitive += int(result.get("omitted_sensitive", 0) or 0)
    return {
        "enabled": True,
        "stop_reason": looped.stop_reason,
        "steps": looped.steps,
        "tool_names": list(looped.tool_names),
        "usage": looped.usage,
        "fallback_candidate": not bool(looped.final_text.strip()),
        "fallback_used": False,
        "workspace_redactions": workspace_redactions,
        "workspace_omitted_sensitive": workspace_omitted_sensitive,
        "calls": [
            {
                "tool_name": outcome.name,
                "status": outcome.status.value,
                "risk_tier": outcome.tier.value if outcome.tier is not None else None,
                "step_index": index,
                "provider_call_id": outcome.call_id,
                "duration_ms": outcome.duration_ms,
                "input": outcome.arguments,
                "output": outcome.result,
                "error": outcome.error_code or outcome.message,
            }
            for index, outcome in enumerate(looped.outcomes, start=1)
        ],
    }


class WorkModelRuntime:
    """Run one Work model turn under a shared budget and role prompt."""

    def __init__(
        self,
        *,
        generate: Callable[..., Awaitable[Any]],
        tool_runner: WorkToolRunner | None,
        model_budget: AgentLoopBudget,
        is_valid_response: Callable[[str], bool],
    ) -> None:
        self.generate = generate
        self.tool_runner = tool_runner
        self.model_budget = model_budget
        self._is_valid_response = is_valid_response

    async def interpret(
        self,
        model_context: dict[str, Any],
        *,
        preferred_profile_id: str | None,
        model: str | None,
        execution_context: AgentExecutionContext | None = None,
        allow_tools: bool = True,
    ) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        """Produce the Work JSON contract, preferring the bounded tool loop.

        The loop is an enrichment, not a replacement: a tool-capable model may
        read extra state first, but if the loop produces nothing usable (budget,
        provider error, or a non-JSON answer) this falls back to the single-shot
        call, which is the path that guarantees JSON mode today.
        """

        started = time.monotonic()
        work_context = model_context.get("work_context")
        role = (
            str(work_context.get("agent_role", WorkAgentRole.PREPROCESSING.value))
            if isinstance(work_context, dict)
            else WorkAgentRole.PREPROCESSING.value
        )
        system_prompt = work_system_prompt(role)
        spent_steps = 0
        spent_tokens = 0
        loop_metadata: dict[str, Any] | None = None
        tools_available = False
        if allow_tools and self.tool_runner is not None:
            try:
                tools_available = await asyncio.wait_for(
                    self.tool_runner.available(preferred_profile_id),
                    timeout=self.model_budget.max_seconds,
                )
            except TimeoutError:
                return "", {"stop_reason": "budget_time"}, None
        if allow_tools and self.tool_runner is not None and tools_available:
            remaining_seconds = self.model_budget.max_seconds - (time.monotonic() - started)
            if remaining_seconds <= 0:
                return "", {"stop_reason": "budget_time"}, None
            looped = await self.tool_runner.run(
                question=str(model_context["question"]),
                recent_messages=model_context["recent_messages"],
                pinned_context=model_context["pinned_context"],
                conversation_summary=model_context["conversation_summary"],
                work_context=model_context["work_context"],
                preferred_profile_id=preferred_profile_id,
                model=model,
                execution_context=execution_context,
                context_window_tokens=int(model_context.get("context_window_tokens", 16_384)),
                max_output_tokens=int(model_context.get("max_output_tokens", 2_048)),
                request_budget=replace(self.model_budget, max_seconds=remaining_seconds),
                system_prompt=system_prompt,
            )
            loop_metadata = _work_loop_metadata(looped)
            spent_steps = looped.steps
            spent_tokens = (
                looped.usage.get("total_tokens", 0)
                or (looped.usage.get("prompt_tokens", 0) + looped.usage.get("completion_tokens", 0))
                or max(1, looped.steps)
                * (looped.estimated_tokens + RegexTokenCounter().count(looped.final_text))
            )
            if spent_tokens > self.model_budget.max_total_tokens:
                return "", {"stop_reason": "budget_tokens", "usage": looped.usage}, loop_metadata
            if looped.stop_reason in {
                "provider_timeout",
                "budget_time",
                "provider_error",
                "cancelled",
                "budget_steps",
                "budget_tokens",
            }:
                return "", {"stop_reason": looped.stop_reason, "usage": looped.usage}, loop_metadata
            if looped.finish_reason == "length":
                # Never spend another complete request repeating a known
                # completion-budget failure. Response validation records it.
                return (
                    looped.final_text,
                    {
                        "profile_id": looped.profile_id,
                        "model": looped.model,
                        "finish_reason": looped.finish_reason,
                        "usage": looped.usage,
                    },
                    loop_metadata,
                )
            if looped.final_text.strip():
                metadata = {
                    "profile_id": looped.profile_id,
                    "model": looped.model,
                    "context_hash": looped.context_hash,
                    "redaction_count": looped.redaction_count,
                    "finish_reason": looped.finish_reason,
                    "usage": looped.usage,
                }
                if self._is_valid_response(looped.final_text):
                    return looped.final_text, metadata, loop_metadata
                # The model answered but not in the required structure; keep the
                # tool evidence recorded and let the single-shot path retry.
                logger.warning("Work tool loop answer failed the JSON contract; retrying once")

        fallback_context = {
            key: value
            for key, value in model_context.items()
            if key not in {"context_window_tokens", "max_output_tokens"}
        }
        if loop_metadata is not None:
            fallback_context["work_context"] = {
                **fallback_context.get("work_context", {}),
                "successful_read_evidence": [
                    call.get("output")
                    for call in loop_metadata.get("calls", [])
                    if call.get("tool_name") == "read_workspace_content"
                    and call.get("status") == "succeeded"
                ][-5:],
            }
            fallback_context["question"] = str(fallback_context["question"]) + (
                "\n本轮只重试一次结构化回复：返回简洁的 answer、action 和增量 draft_patch，"
                "省略没有内容的可选字段。"
                "prepare_preprocessing_plan 只能作为最终 JSON 的 "
                "action 字段值，"
                "prepare_statistical_analysis 也只能作为最终 JSON 的 "
                "action 字段值，"
                "它不是工具；"
                "只引用 successful_read_evidence 中逐字提供的 path 和 locator。"
            )
        remaining_seconds = self.model_budget.max_seconds - (time.monotonic() - started)
        reason = None
        if remaining_seconds <= 0:
            reason = "budget_time"
        elif spent_steps >= self.model_budget.max_steps:
            reason = "budget_steps"
        elif (
            spent_tokens
            + RegexTokenCounter().count(fallback_context)
            + int(model_context.get("max_output_tokens", 2048))
            > self.model_budget.max_total_tokens
        ):
            reason = "budget_tokens"
        if reason:
            return "", {"stop_reason": reason}, loop_metadata
        try:
            generated = await asyncio.wait_for(
                self.generate(
                    **fallback_context,
                    evidence=[],
                    preferred_profile_id=preferred_profile_id,
                    model=model,
                    allow_web_search=False,
                    work_mode=True,
                    system_prompt=system_prompt,
                ),
                timeout=remaining_seconds,
            )
        except TimeoutError:
            return "", {"stop_reason": "provider_timeout"}, loop_metadata
        metadata = {
            "profile_id": generated.selected_profile_id,
            "model": generated.response.model,
            "context_hash": generated.context_hash,
            "redaction_count": generated.redaction_count,
            "finish_reason": generated.response.finish_reason,
            "usage": generated.response.usage,
        }
        fallback_tokens = (
            generated.response.usage.get("total_tokens", 0)
            or generated.response.usage.get("prompt_tokens", 0)
            + generated.response.usage.get("completion_tokens", 0)
            or RegexTokenCounter().count(fallback_context)
            + RegexTokenCounter().count(generated.response.content)
        )
        metadata["request_budget"] = {
            "model_steps": spent_steps + 1,
            "total_tokens": spent_tokens + fallback_tokens,
            "max_steps": self.model_budget.max_steps,
            "max_total_tokens": self.model_budget.max_total_tokens,
        }
        if loop_metadata is not None:
            loop_metadata = {
                **loop_metadata,
                "fallback_used": True,
                "fallback_reason": "tool_loop_unusable_or_incomplete",
                "request_model_steps": spent_steps + 1,
                "request_total_tokens": spent_tokens + fallback_tokens,
            }
        if spent_tokens + fallback_tokens > self.model_budget.max_total_tokens:
            metadata["stop_reason"] = "budget_tokens"
            return "", metadata, loop_metadata
        return generated.response.content, metadata, loop_metadata
