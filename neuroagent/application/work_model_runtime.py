"""Model execution and budget policy for Work role turns."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from neuroagent.agent.gateway import ChatGatewayResult
from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.application.work_prompts import work_system_prompt
from neuroagent.application.work_tools import (
    AgentExecutionContext,
    PersistentModelCallBudget,
    WorkToolRunner,
)
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
        response_repair_profile_id: str | None = None,
    ) -> None:
        self.generate = generate
        self.tool_runner = tool_runner
        self.model_budget = model_budget
        self._is_valid_response = is_valid_response
        self.response_repair_profile_id = response_repair_profile_id
        # Kept as an instance hook so elapsed-budget boundaries are deterministic
        # in tests without changing asyncio's own timeout clock.
        self._clock = time.monotonic

    async def interpret(
        self,
        model_context: dict[str, Any],
        *,
        preferred_profile_id: str | None,
        model: str | None,
        execution_context: AgentExecutionContext | None = None,
        allow_tools: bool = True,
        persistent_model_budget: PersistentModelCallBudget | None = None,
    ) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        """Produce the Work JSON contract, preferring the bounded tool loop.

        The loop is an enrichment, not a replacement: a tool-capable model may
        read extra state first, but if the loop produces nothing usable (budget,
        provider error, or a non-JSON answer) this falls back to the single-shot
        call, which is the path that guarantees JSON mode today.
        """

        turn_started = self._clock()
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
            remaining_seconds = self.model_budget.max_seconds - (self._clock() - turn_started)
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
                persistent_model_budget=persistent_model_budget,
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
            if looped.stop_reason.startswith("composite_goal_"):
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
                loop_response_metadata = {
                    "profile_id": looped.profile_id,
                    "model": looped.model,
                    "context_hash": looped.context_hash,
                    "redaction_count": looped.redaction_count,
                    "finish_reason": looped.finish_reason,
                    "usage": looped.usage,
                }
                if self._is_valid_response(looped.final_text):
                    return looped.final_text, loop_response_metadata, loop_metadata
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
        remaining_seconds = self.model_budget.max_seconds - (self._clock() - turn_started)
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
        reservation = None
        estimated_single_shot_tokens = RegexTokenCounter().count(fallback_context) + int(
            model_context.get("max_output_tokens", 2048)
        )
        estimated_retry_tokens = max(1, estimated_single_shot_tokens)
        persistent_retry_estimate = max(1, math.ceil(estimated_retry_tokens * 1.2))
        if persistent_model_budget is not None:
            reservation = persistent_model_budget.reserve(
                persistent_retry_estimate,
                max(1, math.ceil(remaining_seconds)),
            )
            reservation_status = str(
                getattr(getattr(reservation, "status", None), "value", "") or ""
            )
            if reservation_status != "reserved":
                reason = {
                    "exhausted": "composite_goal_model_budget_exhausted",
                    "duplicate": "composite_goal_model_call_duplicate",
                }.get(reservation_status, "composite_goal_model_reservation_invalid")
                return "", {"stop_reason": reason}, loop_metadata
            remaining_seconds = min(
                remaining_seconds,
                float(reservation.reservation.reserved_elapsed_seconds),
            )
        call_started = self._clock()
        generated: ChatGatewayResult | None = None

        def generated_tokens(result: ChatGatewayResult, estimated_call_tokens: int) -> int:
            usage = result.response.usage
            total = usage.get("total_tokens", 0)
            components = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            actual_tokens = (
                total if total > 0 else components if components > 0 else estimated_call_tokens
            )
            retry_count = max(len(result.attempted_profile_ids) - 1, 0)
            if retry_count:
                actual_tokens += retry_count * estimated_call_tokens
            return actual_tokens

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
            if self._clock() - call_started > remaining_seconds:
                raise TimeoutError("provider response arrived after its elapsed allowance")
        except TimeoutError:
            if reservation is not None and persistent_model_budget is not None:
                persistent_model_budget.settle(
                    reservation,
                    actual_tokens=(
                        generated_tokens(generated, persistent_retry_estimate)
                        if generated is not None
                        else None
                    ),
                    elapsed_seconds=max(1, math.ceil(self._clock() - call_started)),
                )
            return "", {"stop_reason": "provider_timeout"}, loop_metadata
        except BaseException:
            if reservation is not None and persistent_model_budget is not None:
                persistent_model_budget.settle(
                    reservation,
                    actual_tokens=(
                        generated_tokens(generated, persistent_retry_estimate)
                        if generated is not None
                        else None
                    ),
                    elapsed_seconds=max(1, math.ceil(self._clock() - call_started)),
                )
            raise
        assert generated is not None
        fallback_tokens = generated_tokens(generated, estimated_retry_tokens)
        if reservation is not None and persistent_model_budget is not None:
            persistent_model_budget.settle(
                reservation,
                actual_tokens=generated_tokens(generated, persistent_retry_estimate),
                elapsed_seconds=max(1, math.ceil(self._clock() - call_started)),
            )
        metadata: dict[str, Any] = {
            "profile_id": generated.selected_profile_id,
            "model": generated.response.model,
            "context_hash": generated.context_hash,
            "redaction_count": generated.redaction_count,
            "finish_reason": generated.response.finish_reason,
            "usage": generated.response.usage,
        }
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

        initial_content = generated.response.content
        if (
            generated.response.finish_reason == "length"
            or self._is_valid_response(initial_content)
            or not self.response_repair_profile_id
        ):
            return initial_content, metadata, loop_metadata

        # A profile-specific workspace consent must not be widened by schema
        # repair. Send only the current question and role; drop conversation
        # history, pinned/summary context, and any successful workspace reads.
        # Gateway work_mode applies the same outbound rejection/redaction policy
        # as the original model request. WorkDialogue still runs its normal
        # Pydantic and server checks after this one no-tool response.
        repair_context: dict[str, Any] = {
            "question": str(model_context["question"]),
            "recent_messages": [],
            "pinned_context": [],
            "conversation_summary": None,
            "work_context": {"agent_role": role},
        }
        repair_context["question"] = str(repair_context["question"]) + (
            "\n上一条回复未通过 Work JSON 结构校验。请根据本轮原始问题和上下文重新整理，"
            "只返回一个符合 WorkInterpretation 字段结构的 JSON 对象；"
            "不要调用工具、不要执行任何动作，也不要添加 schema 之外的字段。"
        )
        counter = RegexTokenCounter()
        repair_context_tokens = counter.count(repair_context)
        repair_max_output_tokens = int(model_context.get("max_output_tokens", 2048))
        repair_estimate = max(1, repair_context_tokens + repair_max_output_tokens)
        used_steps = spent_steps + 1
        used_tokens = spent_tokens + fallback_tokens
        remaining_seconds = self.model_budget.max_seconds - (self._clock() - turn_started)
        reason = None
        if remaining_seconds <= 0:
            reason = "budget_time"
        elif used_steps >= self.model_budget.max_steps:
            reason = "budget_steps"
        elif used_tokens + repair_estimate > self.model_budget.max_total_tokens:
            reason = "budget_tokens"
        if reason:
            metadata["stop_reason"] = reason
            return "", metadata, loop_metadata

        repair_reservation = None
        persistent_repair_estimate = max(1, math.ceil(repair_estimate * 1.2))
        if persistent_model_budget is not None:
            repair_reservation = persistent_model_budget.reserve(
                persistent_repair_estimate,
                max(1, math.ceil(remaining_seconds)),
            )
            reservation_status = str(
                getattr(getattr(repair_reservation, "status", None), "value", "") or ""
            )
            if reservation_status != "reserved":
                reason = {
                    "exhausted": "composite_goal_model_budget_exhausted",
                    "duplicate": "composite_goal_model_call_duplicate",
                }.get(reservation_status, "composite_goal_model_reservation_invalid")
                metadata["stop_reason"] = reason
                return "", metadata, loop_metadata
            remaining_seconds = min(
                remaining_seconds,
                float(repair_reservation.reservation.reserved_elapsed_seconds),
            )

        repair_started = self._clock()
        repaired: ChatGatewayResult | None = None

        def settle_repair() -> int:
            charged = (
                generated_tokens(repaired, repair_estimate)
                if repaired is not None
                else repair_estimate
            )
            if repair_reservation is not None and persistent_model_budget is not None:
                persistent_model_budget.settle(
                    repair_reservation,
                    actual_tokens=(
                        generated_tokens(repaired, persistent_repair_estimate)
                        if repaired is not None
                        else persistent_repair_estimate
                    ),
                    elapsed_seconds=max(1, math.ceil(self._clock() - repair_started)),
                )
            return charged

        def repair_audit(repair_tokens: int, *, attempted: bool) -> None:
            if loop_metadata is None:
                return
            loop_metadata.update(
                {
                    "json_repair_used": attempted,
                    "request_model_steps": used_steps + int(attempted),
                    "request_total_tokens": used_tokens + repair_tokens,
                }
            )

        repair_call_started = False
        try:
            repair_call_started = True
            repaired = await asyncio.wait_for(
                self.generate(
                    **repair_context,
                    evidence=[],
                    preferred_profile_id=self.response_repair_profile_id,
                    model=None,
                    allow_web_search=False,
                    work_mode=True,
                    system_prompt=system_prompt,
                ),
                timeout=remaining_seconds,
            )
            if self._clock() - repair_started > remaining_seconds:
                raise TimeoutError("JSON repair arrived after its elapsed allowance")
        except TimeoutError:
            repair_tokens = settle_repair() if repair_call_started else 0
            metadata["request_budget"] = {
                **metadata["request_budget"],
                "model_steps": used_steps + int(repair_call_started),
                "total_tokens": used_tokens + repair_tokens,
            }
            metadata["stop_reason"] = "provider_timeout"
            repair_audit(repair_tokens, attempted=repair_call_started)
            return "", metadata, loop_metadata
        except asyncio.CancelledError:
            if repair_call_started:
                settle_repair()
            raise
        except Exception:
            repair_tokens = settle_repair() if repair_call_started else 0
            metadata["request_budget"] = {
                **metadata["request_budget"],
                "model_steps": used_steps + int(repair_call_started),
                "total_tokens": used_tokens + repair_tokens,
            }
            metadata["stop_reason"] = "provider_error"
            repair_audit(repair_tokens, attempted=repair_call_started)
            return "", metadata, loop_metadata

        assert repaired is not None
        repair_tokens = settle_repair()
        request_steps = used_steps + 1
        request_tokens = used_tokens + repair_tokens
        metadata.update(
            {
                "profile_id": repaired.selected_profile_id,
                "model": repaired.response.model,
                "context_hash": repaired.context_hash,
                "redaction_count": repaired.redaction_count,
                "finish_reason": repaired.response.finish_reason,
                "usage": repaired.response.usage,
                "json_repair": {
                    "attempted": True,
                    "profile_id": repaired.selected_profile_id,
                    "model": repaired.response.model,
                    "finish_reason": repaired.response.finish_reason,
                    "usage": repaired.response.usage,
                    "valid": self._is_valid_response(repaired.response.content),
                },
                "request_budget": {
                    **metadata["request_budget"],
                    "model_steps": request_steps,
                    "total_tokens": request_tokens,
                },
            }
        )
        repair_audit(repair_tokens, attempted=True)
        if request_tokens > self.model_budget.max_total_tokens:
            metadata["stop_reason"] = "budget_tokens"
            return "", metadata, loop_metadata
        return repaired.response.content, metadata, loop_metadata
