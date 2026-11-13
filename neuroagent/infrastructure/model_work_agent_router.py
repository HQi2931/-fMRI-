"""Rule-first Work Agent routing with a small-model and large-model fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictFloat

from neuroagent.agent.gateway import WORK_AGENT_ROUTING_SYSTEM_PROMPT
from neuroagent.agent.redaction import OutboundPolicyError, reject_sensitive_work_context
from neuroagent.application.errors import ApplicationError
from neuroagent.application.work_agents import is_analysis_goal_supplement
from neuroagent.application.work_routing import (
    WorkAgentRouteChoice,
    WorkAgentRoutingContext,
    WorkAgentRoutingDecision,
    WorkAgentRoutingStatus,
)
from neuroagent.application.work_tools import PersistentModelCallBudget
from neuroagent.context.engine import RegexTokenCounter
from neuroagent.multi_agent.registry import WorkAgentRole

logger = logging.getLogger(__name__)

_WINDOWS_PATH = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:\\|\\\\)[^\r\n\"'<>|?*]*?"
    r"\.(?:nii(?:\.gz)?|img|hdr|tsv|csv|json|txt|mat|zip|png|jpe?g|dcm|gz)"
    r"(?=$|[\s,;)}\]])"
)
_POSIX_PATH = re.compile(
    r"(?i)(?<![\w:/])/(?:[^/\r\n\"'<>|]+/)*[^/\r\n\"'<>|]+?"
    r"\.(?:nii(?:\.gz)?|img|hdr|tsv|csv|json|txt|mat|zip|png|jpe?g|dcm|gz)"
    r"(?=$|[\s,;)}\]])"
)
_ABSOLUTE_PATH_LINE = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:\\|\\\\)[^\r\n\"'<>|]+|"
    r"(?<![\w:/])/(?:home|Users|mnt|data|tmp)/[^\r\n\"'<>|]+"
)
_SUBJECT_ID = re.compile(r"(?i)\b(?:sub|subject|participant)[-_ ]?\d+[a-z]?\b")
_GROUP_LABEL = re.compile(
    r"(?i)\b(?:ASD|HC|MDD|BD|OCD|ADHD|PTSD|SZ|SCZ|controls?|patients?|cases?)"
    r"(?:[- ]?(?:group|arm))?\b|(?:实验组|对照组|病例组|患者组|健康对照组|健康组)"
)
_DATA_FILENAME = re.compile(
    r"(?i)\b(?:participants?\.(?:tsv|csv)|[^\s/\\]+\.(?:nii(?:\.gz)?|img|hdr|png|jpe?g|dcm))\b"
)
_CLEAR_PREPROCESSING = re.compile(
    r"预处理(?:方案|计划|流程|草稿|失败|报错|日志|参数|设置|检查)?|DPABI|"
    r"(?:计算|生成|提取|运行|执行|准备|检查|质控).{0,16}(?:ALFF|fALFF|ReHo|指标|预处理)|"
    r"滤波|平滑|配准|标准化|头动校正|质控|QC|输入阶段|"
    r"\b(?:preprocess(?:ing)? plan|preprocess(?:ing)?|motion correction|"
    r"normalization|registration|filtering|smoothing)\b",
    re.IGNORECASE,
)
_CLEAR_ANALYSIS = re.compile(
    r"统计分析|统计比较|组间比较|组间差异|两组比较|多组比较|t\s*检验|FDR|GRF|"
    r"相关分析|回归分析|ANOVA|ANCOVA|显著性|结果解释|ROI分析|"
    r"\b(?:statistical analysis|group comparison|group difference|t[ -]?test|"
    r"correlation analysis|regression analysis|ANOVA|ANCOVA|FDR|GRF)\b",
    re.IGNORECASE,
)
_CONTINUATION = re.compile(
    r"^(?:继续|接着|下一步|查看进度|查看结果|刷新|重试|再试一次|"
    r"continue|next(?: step)?|show(?: the)? (?:results?|progress)|retry)[。！!？?. ]*$",
    re.IGNORECASE,
)


class _WorkAgentRouteResponse(BaseModel):
    """Strict response contract for route-only model calls."""

    model_config = ConfigDict(extra="forbid")

    agent_role: WorkAgentRouteChoice
    confidence: StrictFloat = Field(ge=0, le=1)


def prepare_work_routing_text(text: str) -> str:
    """Reject pasted subject/image data and remove local identifiers from prose."""

    try:
        reject_sensitive_work_context({"request_text": text})
    except OutboundPolicyError as exc:
        raise ApplicationError(
            "work_agent_router_sensitive_context_blocked",
            "本轮文本包含疑似受试者表格、组别对应或影像内容；此类内容不会发送给路由模型。",
            status_code=422,
        ) from exc
    safe = _WINDOWS_PATH.sub("[本地路径已隐藏]", text)
    safe = _POSIX_PATH.sub("[本地路径已隐藏]", safe)
    safe = _ABSOLUTE_PATH_LINE.sub("[本地路径已隐藏]", safe)
    safe = _DATA_FILENAME.sub("[本地数据文件已隐藏]", safe)
    safe = _SUBJECT_ID.sub("[受试者编号已隐藏]", safe)
    return _GROUP_LABEL.sub("组别", safe)


def route_work_agent_by_rules(
    request_text: str, context: WorkAgentRoutingContext
) -> WorkAgentRoutingDecision | None:
    """Handle only clear requests locally; leave overlap and uncertainty to models."""

    if context.required_initial_role is not None:
        role = context.required_initial_role
        return _rule_decision(role, "required_initial_role")
    normalized = request_text.strip()
    if _CONTINUATION.fullmatch(normalized):
        role = (
            WorkAgentRole.ANALYSIS
            if context.has_selected_analysis_goal
            else context.current_agent_role
        )
        return _rule_decision(role, "conversation_continuation")
    if (
        context.current_agent_role is WorkAgentRole.ANALYSIS
        and context.has_selected_analysis_goal
        and is_analysis_goal_supplement(normalized)
    ):
        return _rule_decision(WorkAgentRole.ANALYSIS, "selected_analysis_supplement")
    preprocessing = bool(_CLEAR_PREPROCESSING.search(normalized))
    analysis = bool(_CLEAR_ANALYSIS.search(normalized))
    if preprocessing == analysis:
        return None
    role = WorkAgentRole.PREPROCESSING if preprocessing else WorkAgentRole.ANALYSIS
    return _rule_decision(role, "clear_request_rule")


def _rule_decision(role: WorkAgentRole, reason: str) -> WorkAgentRoutingDecision:
    choice = (
        WorkAgentRouteChoice.PREPROCESSING
        if role is WorkAgentRole.PREPROCESSING
        else WorkAgentRouteChoice.ANALYSIS
    )
    return WorkAgentRoutingDecision(
        status=WorkAgentRoutingStatus.ROUTED,
        role=role,
        choice=choice,
        confidence=1.0,
        model="rules",
        reason=reason,
        source="rule",
    )


class ModelWorkAgentRouter:
    """Run deterministic rules, a small model, then a configured large fallback."""

    def __init__(
        self,
        *,
        generate: Callable[..., Awaitable[Any]],
        small_profile_id: str,
        fallback_profile_id: str,
        timeout_seconds: float = 20.0,
        minimum_confidence: float = 0.65,
        max_request_tokens: int = 4_096,
        max_request_elapsed_seconds: int = 45,
    ) -> None:
        self._generate = generate
        self.small_profile_id = small_profile_id.strip()
        self.fallback_profile_id = fallback_profile_id.strip()
        self.timeout_seconds = timeout_seconds
        self.minimum_confidence = minimum_confidence
        self.max_request_tokens = max_request_tokens
        self.max_request_elapsed_seconds = max_request_elapsed_seconds
        self._token_counter = RegexTokenCounter()

    async def choose(
        self,
        *,
        request_text: str,
        context: WorkAgentRoutingContext,
        persistent_model_budget: PersistentModelCallBudget | None,
    ) -> WorkAgentRoutingDecision:
        safe_text = prepare_work_routing_text(request_text.strip())
        ruled = route_work_agent_by_rules(request_text, context)
        if ruled is not None:
            return ruled
        if not self.small_profile_id or not self.fallback_profile_id:
            raise ApplicationError(
                "work_agent_router_not_configured",
                "Work Agent 路由模型尚未配置；请检查小模型和兜底模型 Profile ID。",
                status_code=503,
            )

        route_context: dict[str, object] = {
            "current_agent": context.current_agent_role.value,
            "has_project": context.has_project,
            "has_bound_preprocessing_run": context.has_bound_preprocessing_run,
            "has_selected_analysis_goal": context.has_selected_analysis_goal,
            "server_required_initial_agent": (
                context.required_initial_role.value
                if context.required_initial_role is not None
                else None
            ),
        }
        estimated_tokens = max(
            128,
            self._token_counter.count(
                {
                    "question": safe_text,
                    "work_context": route_context,
                    "prompt": WORK_AGENT_ROUTING_SYSTEM_PROMPT,
                }
            )
            + 128,
        )
        if estimated_tokens > self.max_request_tokens:
            raise ApplicationError(
                "work_agent_router_budget_exhausted",
                "当前请求超出 Work Agent 路由模型预算，尚未生成方案。",
                status_code=429,
            )

        spent_estimate = 0
        started = time.monotonic()
        primary, primary_error = await self._call_profile(
            profile_id=self.small_profile_id,
            request_text=safe_text,
            route_context=route_context,
            persistent_model_budget=persistent_model_budget,
            estimated_tokens=estimated_tokens,
            attempt_index=1,
        )
        spent_estimate += estimated_tokens
        if primary is not None and self._is_confident(primary):
            return primary

        # A single configured, distinct large model is the deliberate fallback.
        if self.fallback_profile_id == self.small_profile_id:
            if primary is not None:
                return self._clarification(primary, "fallback_profile_matches_small")
            assert primary_error is not None
            raise ApplicationError(
                "work_agent_router_unavailable",
                "Work Agent 小模型路由失败，且兜底 Profile 与小模型相同。",
                status_code=503,
            ) from None
        if spent_estimate + estimated_tokens > self.max_request_tokens:
            if primary is not None:
                return self._clarification(primary, "route_budget_exhausted_after_small_model")
            raise ApplicationError(
                "work_agent_router_budget_exhausted",
                "小模型路由失败后，剩余路由预算不足以调用兜底模型。",
                status_code=429,
            ) from None
        if time.monotonic() - started >= self.max_request_elapsed_seconds:
            if primary is not None:
                return self._clarification(primary, "route_time_budget_exhausted_after_small_model")
            raise ApplicationError(
                "work_agent_router_budget_exhausted",
                "小模型路由失败后，剩余时间不足以调用兜底模型。",
                status_code=429,
            ) from None

        fallback, fallback_error = await self._call_profile(
            profile_id=self.fallback_profile_id,
            request_text=safe_text,
            route_context=route_context,
            persistent_model_budget=persistent_model_budget,
            estimated_tokens=estimated_tokens,
            attempt_index=2,
        )
        if (
            fallback is not None
            and self._is_confident(fallback)
            and fallback.status is WorkAgentRoutingStatus.NEEDS_CLARIFICATION
        ):
            return self._clarification(fallback, "large_model_uncertain")
        if fallback is not None and self._is_confident(fallback):
            return WorkAgentRoutingDecision(
                status=fallback.status,
                role=fallback.role,
                choice=fallback.choice,
                confidence=fallback.confidence,
                model=fallback.model,
                reason="large_model_fallback",
                source="large_model_fallback",
                profile_id=fallback.profile_id,
            )
        if fallback is not None:
            return self._clarification(fallback, "large_model_uncertain")
        if primary is not None:
            return self._clarification(primary, "large_model_fallback_unavailable")
        logger.warning(
            "Work Agent routing models unavailable: small=%s fallback=%s",
            _error_code(primary_error),
            _error_code(fallback_error),
        )
        raise ApplicationError(
            "work_agent_router_unavailable",
            "小模型和兜底模型均未能完成 Work Agent 分派；未生成方案或启动操作。",
            status_code=503,
        ) from None

    def _is_confident(self, decision: WorkAgentRoutingDecision) -> bool:
        return (
            decision.confidence is not None
            and decision.confidence >= self.minimum_confidence
            and (
                (decision.status is WorkAgentRoutingStatus.ROUTED and decision.role is not None)
                or (
                    decision.status is WorkAgentRoutingStatus.NEEDS_CLARIFICATION
                    and decision.choice is WorkAgentRouteChoice.CLARIFY
                )
            )
        )

    def _clarification(
        self, decision: WorkAgentRoutingDecision, reason: str
    ) -> WorkAgentRoutingDecision:
        return WorkAgentRoutingDecision(
            status=WorkAgentRoutingStatus.NEEDS_CLARIFICATION,
            role=None,
            choice=WorkAgentRouteChoice.CLARIFY,
            confidence=decision.confidence,
            model=decision.model,
            reason=reason,
            source=decision.source,
            profile_id=decision.profile_id,
        )

    async def _call_profile(
        self,
        *,
        profile_id: str,
        request_text: str,
        route_context: dict[str, object],
        persistent_model_budget: PersistentModelCallBudget | None,
        estimated_tokens: int,
        attempt_index: int,
    ) -> tuple[WorkAgentRoutingDecision | None, ApplicationError | Exception | None]:
        reservation = None
        if persistent_model_budget is not None:
            reservation = persistent_model_budget.reserve(
                estimated_tokens, max(1, math.ceil(self.timeout_seconds))
            )
            reservation_status = str(
                getattr(getattr(reservation, "status", None), "value", "") or ""
            )
            if reservation_status != "reserved":
                code = {
                    "exhausted": "composite_goal_model_budget_exhausted",
                    "duplicate": "composite_goal_model_call_duplicate",
                }.get(reservation_status, "composite_goal_model_reservation_invalid")
                raise ApplicationError(
                    code,
                    "复合目标持久模型预算不足以执行 Work Agent 路由。",
                    status_code=409,
                )
        started = time.monotonic()
        result: Any = None
        caught: BaseException | None = None
        try:
            result = await asyncio.wait_for(
                self._generate(
                    question=request_text,
                    evidence=[],
                    recent_messages=[],
                    pinned_context=[],
                    conversation_summary=None,
                    work_context=route_context,
                    preferred_profile_id=profile_id,
                    model=None,
                    allow_web_search=False,
                    work_agent_routing=True,
                    system_prompt=WORK_AGENT_ROUTING_SYSTEM_PROMPT,
                ),
                timeout=self.timeout_seconds,
            )
            decision = self._parse_response(
                result, profile_id=profile_id, attempt_index=attempt_index
            )
        except asyncio.CancelledError as exc:
            caught = exc
            decision = None
        except Exception as exc:
            caught = exc
            decision = None
        finally:
            if reservation is not None and persistent_model_budget is not None:
                usage = _result_usage(result)
                try:
                    persistent_model_budget.settle(
                        reservation,
                        actual_tokens=_usage_tokens(usage),
                        elapsed_seconds=max(1, math.ceil(time.monotonic() - started)),
                    )
                except Exception as settlement_error:
                    if caught is None:
                        caught = settlement_error
                        decision = None
        if isinstance(caught, asyncio.CancelledError):
            raise caught
        if caught is not None:
            logger.warning(
                "Work Agent route attempt failed: profile=%s code=%s type=%s",
                profile_id,
                _error_code(caught),
                type(caught).__name__,
            )
            return None, caught if isinstance(caught, Exception) else RuntimeError("route failed")
        return decision, None

    def _parse_response(
        self, result: Any, *, profile_id: str, attempt_index: int
    ) -> WorkAgentRoutingDecision:
        response = getattr(result, "response", None)
        content = getattr(response, "content", None)
        if not isinstance(content, str):
            raise ValueError("route model response is missing text")
        try:
            payload = json.loads(content)
        except (TypeError, ValueError) as exc:
            raise ValueError("route model response is not JSON") from exc
        route_response = _WorkAgentRouteResponse.model_validate(payload)
        choice = route_response.agent_role
        confidence = float(route_response.confidence)
        model = getattr(response, "model", None)
        model_name = model if isinstance(model, str) and model else profile_id
        source = "small_model" if attempt_index == 1 else "large_model_fallback"
        if choice is WorkAgentRouteChoice.CLARIFY:
            return WorkAgentRoutingDecision(
                status=WorkAgentRoutingStatus.NEEDS_CLARIFICATION,
                role=None,
                choice=choice,
                confidence=confidence,
                model=model_name,
                reason="model_clarify",
                source=source,
                profile_id=profile_id,
            )
        role = (
            WorkAgentRole.PREPROCESSING
            if choice is WorkAgentRouteChoice.PREPROCESSING
            else WorkAgentRole.ANALYSIS
        )
        return WorkAgentRoutingDecision(
            status=WorkAgentRoutingStatus.ROUTED,
            role=role,
            choice=choice,
            confidence=confidence,
            model=model_name,
            source=source,
            profile_id=profile_id,
        )


def _result_usage(result: Any) -> Mapping[str, Any]:
    response = getattr(result, "response", None)
    usage = getattr(response, "usage", None)
    return usage if isinstance(usage, Mapping) else {}


def _usage_tokens(usage: Mapping[str, Any]) -> int | None:
    prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
    if (
        isinstance(prompt_tokens, int)
        and not isinstance(prompt_tokens, bool)
        and prompt_tokens >= 0
        and isinstance(completion_tokens, int)
        and not isinstance(completion_tokens, bool)
        and completion_tokens >= 0
    ):
        return prompt_tokens + completion_tokens
    return None


def _error_code(error: BaseException | None) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else type(error).__name__ if error else "none"
