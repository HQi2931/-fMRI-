"""Redacted, schema-checked access to configured model providers."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from neuroagent.agent.models import (
    AgentTaskRequest,
    GatewayResult,
    ModelCapability,
    ModelProfile,
    ProviderResponse,
    StructuredRecommendation,
)
from neuroagent.agent.providers import ModelProvider, ProviderError, RetryableProviderError
from neuroagent.agent.redaction import OutboundContextPolicy, reject_sensitive_work_context
from neuroagent.agent.router import ModelRouter
from neuroagent.agent.secrets import SecretResolver
from neuroagent.agent.streaming import current_answer_stream
from neuroagent.agent.tool_spec import LlmTool
from neuroagent.observability.work_timings import measure_work_stage


class ModelGatewayError(RuntimeError):
    pass


class ModelGatewayTimeoutError(ModelGatewayError):
    timeout = True


@dataclass(frozen=True)
class ToolTurnResult:
    """One provider turn taken inside the bounded tool loop."""

    response: ProviderResponse
    selected_profile_id: str
    attempted_profile_ids: tuple[str, ...]


@dataclass(frozen=True)
class ChatMessages:
    """Redacted chat payload rendered into provider-ready messages."""

    messages: list[dict[str, str]]
    payload: dict[str, object]
    context_hash: str
    redaction_count: int


_CHAT_SYSTEM_PROMPT = (
    "你是 rs-fMRI 科研文字分析助手。只回答 rs-fMRI、DPABI、SPM、"
    "影像统计设计和相关参数问题。优先使用给出的本地证据; "
    "证据不足时明确说明不确定性, 不得编造科研结论、参数默认值或引用。"
    "回答使用中文, 并区分通用方法信息与用户项目事实。"
    "面向用户只输出自然语言, 不要输出内部 ID、UUID、哈希、trace 或工具字段; "
    "只有用户明确要求时才解释这些内部信息的用途。"
)

_CHAT_CITATION_PROMPT = (
    "允许简短回应寒暄。历史消息用于理解追问, 不视为文献证据。"
    "引用本地证据时必须使用其 citation_id, 例如 [C1]; 只引用实际支持结论的片段。"
    "无本地证据时说明未找到文献依据, 一般解释不得冒充文献结论。"
)

_ROUTING_SYSTEM_PROMPT = (
    "根据当前问题与历史判断意图, 并将科研追问改写为独立检索问题。只返回 JSON: "
    '{"intent":"knowledge_query|conversation|work_request","query":"独立检索问题",'
    '"work_request":null}。执行需求的 work_request 为 {"task":"任务名",'
    '"parameters":{}}。只生成草案; 方法咨询不是执行请求。'
    "不要回答问题, 不要编造历史未提供的信息。"
    "查找方案、检索文献、解释步骤及否定执行都是知识咨询；"
    "提到预处理或运行不等于请求执行。只有明确要求实际处理数据才是 work_request。"
)

WORK_AGENT_ROUTING_SYSTEM_PROMPT = (
    "将当前 Work 请求路由到 preprocessing、analysis 或 clarify。"
    "preprocessing 负责数据检查、预处理方案、DPABI/SPM 预处理执行与质控故障；"
    "analysis 负责统计设计、指标结果解释、组间比较与统计故障；"
    "如果信息不足、同时涉及两个阶段但未说明顺序，或不确定，就选择 clarify。"
    "server_required_initial_agent 若不为空，必须选择它。只根据当前请求和提供的少量状态判断；"
    "不得声称已执行任务，不得生成方案或采取行动。只返回 JSON 对象，字段严格为："
    '{"agent_role":"preprocessing|analysis|clarify","confidence":0 到 1 的数值}。'
)

_WORK_JSON_SYSTEM_PROMPT = "Return a JSON object with an answer and an action."


_SUMMARY_SYSTEM_PROMPT = (
    "将较早的对话压缩为简洁中文摘要, 保留用户目标、已明确偏好、已作决定和"
    '未解决问题。不要添加新事实。只返回 JSON: {"summary":"..."}。'
)

_MEMORY_SYSTEM_PROMPT = (
    "Extract only durable user preferences, instructions, project facts, decisions, "
    "and explicitly chosen scientific parameters from the current user message. "
    "Return JSON with candidates, an array of objects containing kind "
    "(preference|instruction|scientific_parameter|project_fact|decision), key "
    "(stable lowercase identifier), "
    "content, scope (conversation|project), confidence (0..1), importance (0..1). "
    "Do not infer facts, copy questions, store secrets or identifiers, or treat "
    "assistant text as user memory. Use project scope only when the user explicitly "
    "asks for project-wide "
    "reuse. Return an empty array when nothing should persist."
)


@dataclass(frozen=True)
class ChatGatewayResult:
    response: ProviderResponse
    selected_profile_id: str
    context_hash: str
    attempted_profile_ids: tuple[str, ...]
    remote_search_used: bool
    redaction_count: int


class ModelGateway:
    _system_prompt = (
        "You assist a research workflow. Return one JSON object with these fields: "
        "summary (string), proposed_skill_request (object or null), warnings (string array), "
        "unresolved_questions (string array), requires_user_confirmation (boolean), and for "
        "workspace format advice optionally expected_layout (string array), adjustment_steps "
        "(string array), recommended_target_stage (a single safe DPABI stage name such as "
        "FunRaw, FunImgAR, FunImgARW, FunImgARWS, or null). For Work preprocessing-stage intent, "
        "return recommended_input_stage (only from the server-provided workspace stage "
        "candidates), stage_intent, stage_reasoning, and stage_questions. You may only "
        "recommend a stage; the user must confirm it with the server-issued command. "
        "Never emit commands, absolute paths, raw image content, tool calls, workflow "
        "transitions, or invented scientific defaults."
    )

    def __init__(
        self,
        router: ModelRouter,
        providers: Mapping[str, ModelProvider],
        outbound_policy: OutboundContextPolicy,
        secret_resolver: SecretResolver,
    ) -> None:
        self._router = router
        self._providers = providers
        self._outbound_policy = outbound_policy
        self._secret_resolver = secret_resolver

    async def generate(self, request: AgentTaskRequest) -> GatewayResult:
        context = self._outbound_policy.redact(request.summary.model_dump(mode="json"))
        candidates = self._router.candidates(request)
        attempted: list[str] = []
        last_retryable: Exception | None = None
        for profile in candidates:
            provider = self._providers.get(profile.provider)
            if provider is None:
                continue
            api_key = self._secret_resolver.resolve(profile.api_key_env)
            if not api_key:
                continue
            attempted.append(profile.id)
            try:
                recommendation, response = await self._request_structured(
                    provider, profile, api_key, context.payload
                )
            except RetryableProviderError as exc:
                last_retryable = exc
                continue
            decision = self._router.decision(request, candidates, profile)
            return GatewayResult(
                recommendation=recommendation,
                routing=decision,
                context_hash=context.context_hash,
                attempted_profile_ids=tuple(attempted),
                model=response.model,
                redaction_count=context.redaction_count,
            )
        if last_retryable:
            if getattr(last_retryable, "timeout", False):
                raise ModelGatewayTimeoutError(
                    "all providers exceeded their request deadlines"
                ) from last_retryable
            raise ModelGatewayError(
                "all available providers were temporarily unavailable"
            ) from last_retryable
        raise ModelGatewayError("no routed provider has both an adapter and a configured API key")

    async def generate_chat(
        self,
        *,
        question: str,
        evidence: list[dict[str, object]],
        preferred_profile_id: str | None,
        model: str | None,
        allow_web_search: bool,
        recent_messages: list[dict[str, object]] | None = None,
        pinned_context: list[dict[str, object]] | None = None,
        conversation_summary: str | None = None,
        work_context: dict[str, object] | None = None,
        routing: bool = False,
        summary_mode: bool = False,
        memory_mode: bool = False,
        work_mode: bool = False,
        work_agent_routing: bool = False,
        system_prompt: str | None = None,
    ) -> ChatGatewayResult:
        rendered = self.build_chat_messages(
            question=question,
            evidence=evidence,
            recent_messages=recent_messages,
            pinned_context=pinned_context,
            conversation_summary=conversation_summary,
            work_context=work_context,
            routing=routing,
            summary_mode=summary_mode,
            memory_mode=memory_mode,
            work_mode=work_mode,
            work_agent_routing=work_agent_routing,
            system_prompt=system_prompt,
            allow_web_search=allow_web_search,
        )
        messages = rendered.messages
        profiles = list(self._router.profiles.values())
        if preferred_profile_id is not None:
            profiles = [profile for profile in profiles if profile.id == preferred_profile_id]
        else:
            profiles.sort(key=lambda item: item.priority)
        if allow_web_search:
            from neuroagent.agent.models import ModelCapability

            profiles = [
                profile
                for profile in profiles
                if ModelCapability.WEB_SEARCH in profile.capabilities
            ]
        if model is not None:
            profiles = [profile.model_copy(update={"model": model}) for profile in profiles]
        if not profiles:
            requirement = "支持联网搜索的" if allow_web_search else "可用的"
            raise ModelGatewayError(f"no {requirement} model profile is configured")

        messages = rendered.messages
        attempted: list[str] = []
        last_retryable: Exception | None = None
        for profile in profiles:
            provider = self._providers.get(profile.provider)
            if provider is None:
                continue
            api_key = self._secret_resolver.resolve(profile.api_key_env)
            if not api_key:
                continue
            attempted.append(profile.id)
            try:
                response = await self._generate_visible(
                    provider,
                    profile,
                    api_key,
                    messages,
                    web_search=allow_web_search,
                    json_object=(
                        routing or summary_mode or memory_mode or work_mode or work_agent_routing
                    ),
                    visible=not (routing or summary_mode or memory_mode or work_agent_routing),
                )
            except RetryableProviderError as exc:
                last_retryable = exc
                continue
            return ChatGatewayResult(
                response=response,
                selected_profile_id=profile.id,
                context_hash=rendered.context_hash,
                attempted_profile_ids=tuple(attempted),
                remote_search_used=allow_web_search,
                redaction_count=rendered.redaction_count,
            )
        if last_retryable:
            raise ModelGatewayError(
                "all available chat providers were temporarily unavailable"
            ) from last_retryable
        raise ModelGatewayError(
            "no routed chat provider has both an adapter and a configured API key"
        )

    def build_chat_messages(
        self,
        *,
        question: str,
        evidence: list[dict[str, object]],
        preferred_profile_id: str | None = None,
        model: str | None = None,
        allow_web_search: bool = False,
        recent_messages: list[dict[str, object]] | None = None,
        pinned_context: list[dict[str, object]] | None = None,
        conversation_summary: str | None = None,
        work_context: dict[str, object] | None = None,
        routing: bool = False,
        summary_mode: bool = False,
        memory_mode: bool = False,
        work_mode: bool = False,
        work_agent_routing: bool = False,
        system_prompt: str | None = None,
    ) -> ChatMessages:
        """Redact the chat payload and render it into provider messages.

        Both the single-shot path and the bounded tool loop use this so a tool
        turn can never be sent with a weaker prompt or weaker redaction.
        """

        if work_mode or work_agent_routing:
            reject_sensitive_work_context(
                {
                    "question": question,
                    "local_evidence": evidence,
                    "recent_messages": recent_messages or [],
                    "pinned_context": pinned_context or [],
                    "conversation_summary": conversation_summary,
                    "work_context": work_context or {},
                }
            )
        del preferred_profile_id, model
        context = self._outbound_policy.redact(
            {
                "question": question,
                "local_evidence": evidence,
                "recent_messages": recent_messages or [],
                "pinned_context": pinned_context or [],
                "conversation_summary": conversation_summary,
                "work_context": work_context or {},
            }
        )
        resolved_system_prompt = system_prompt
        if resolved_system_prompt is None:
            resolved_system_prompt = _CHAT_SYSTEM_PROMPT + _CHAT_CITATION_PROMPT
        if work_agent_routing:
            resolved_system_prompt = (
                WORK_AGENT_ROUTING_SYSTEM_PROMPT if system_prompt is None else system_prompt
            )
        elif routing:
            resolved_system_prompt = _ROUTING_SYSTEM_PROMPT
        elif work_mode:
            resolved_system_prompt = (
                _WORK_JSON_SYSTEM_PROMPT if system_prompt is None else system_prompt
            )
        elif summary_mode:
            resolved_system_prompt = _SUMMARY_SYSTEM_PROMPT
        elif memory_mode:
            resolved_system_prompt = _MEMORY_SYSTEM_PROMPT
        if allow_web_search:
            resolved_system_prompt += (
                "本次允许使用联网搜索。仅引用与问题直接相关的公开来源, 并在结论旁保留来源引用。"
            )
        messages = [
            {"role": "system", "content": resolved_system_prompt},
            {
                "role": "user",
                "content": json.dumps(context.payload, ensure_ascii=False, sort_keys=True),
            },
        ]
        return ChatMessages(
            messages=messages,
            payload=dict(context.payload),
            context_hash=context.context_hash,
            redaction_count=context.redaction_count,
        )

    async def tool_capable(self, preferred_profile_id: str | None) -> bool:
        """Whether any usable profile declares tool-calling support."""

        return bool(self._tool_profiles(preferred_profile_id, None))

    async def generate_tool_turn(
        self,
        *,
        messages: Sequence[dict[str, object]],
        tools: Sequence[LlmTool],
        preferred_profile_id: str | None,
        model: str | None,
        remaining_seconds: float | None = None,
    ) -> ToolTurnResult:
        """One raw tool-calling turn, used by the bounded Agent loop.

        The caller owns the message transcript and the catalog, so both are
        already redacted and already restricted to model-visible tools.
        """

        if not tools:
            raise ModelGatewayError("a tool turn requires at least one model-visible tool")
        profiles = self._tool_profiles(preferred_profile_id, model)
        attempted: list[str] = []
        last_retryable: Exception | None = None
        for profile in profiles:
            provider = self._providers.get(profile.provider)
            if provider is None:
                continue
            api_key = self._secret_resolver.resolve(profile.api_key_env)
            if not api_key:
                continue
            attempted.append(profile.id)
            try:
                response = await self._generate_visible(
                    provider,
                    profile,
                    api_key,
                    messages,
                    tools=tools,
                    tool_choice="auto",
                    # A tool-calling turn must not also demand JSON mode: the model
                    # has to be free to answer with tool_calls instead of a JSON
                    # object, and several OpenAI-compatible gateways reject the
                    # combination outright.
                    json_object=False,
                    timeout_seconds=(
                        min(profile.timeout_seconds, remaining_seconds)
                        if remaining_seconds is not None
                        else None
                    ),
                )
            except RetryableProviderError as exc:
                last_retryable = exc
                continue
            return ToolTurnResult(
                response=response,
                selected_profile_id=profile.id,
                attempted_profile_ids=tuple(attempted),
            )
        if last_retryable:
            if getattr(last_retryable, "timeout", False):
                raise ModelGatewayTimeoutError(
                    "all tool-use providers exceeded their request deadlines"
                ) from last_retryable
            raise ModelGatewayError(
                "all available tool-use providers were temporarily unavailable"
            ) from last_retryable
        raise ModelGatewayError("no tool-use provider has both an adapter and a configured API key")

    async def _generate_visible(
        self,
        provider: ModelProvider,
        profile: ModelProfile,
        api_key: str,
        messages: Sequence[Mapping[str, object]],
        *,
        visible: bool = True,
        **options: object,
    ) -> ProviderResponse:
        observer = current_answer_stream() if visible else None
        if observer is not None:
            await observer.reset()
            stream = getattr(provider, "generate_stream", None)
            if stream is not None and ModelCapability.STREAMING in profile.capabilities:
                with measure_work_stage("model_response"):
                    response: ProviderResponse = await stream(
                        profile, api_key, messages, on_text=observer.text, **options
                    )
                if response.tool_calls:
                    await observer.reset()
                return response
        with measure_work_stage("model_response"):
            response = await provider.generate(profile, api_key, messages, **options)  # type: ignore[arg-type]
        if observer is not None and not response.tool_calls:
            await observer.text(response.content)
        return response

    def _tool_profiles(
        self, preferred_profile_id: str | None, model: str | None
    ) -> list[ModelProfile]:
        profiles = [
            profile
            for profile in self._router.profiles.values()
            if ModelCapability.TOOL_USE in profile.capabilities
        ]
        if preferred_profile_id is not None:
            profiles = [profile for profile in profiles if profile.id == preferred_profile_id]
        else:
            profiles.sort(key=lambda item: item.priority)
        if model is not None:
            profiles = [profile.model_copy(update={"model": model}) for profile in profiles]
        return profiles

    async def _request_structured(
        self,
        provider: ModelProvider,
        profile: ModelProfile,
        api_key: str,
        payload: Mapping[str, object],
    ) -> tuple[StructuredRecommendation, ProviderResponse]:
        messages = [
            {"role": "system", "content": self._system_prompt},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        ]
        response = await provider.generate(profile, api_key, messages)
        try:
            return StructuredRecommendation.model_validate_json(response.content), response
        except ValidationError:
            repair_messages = [
                *messages,
                {"role": "assistant", "content": response.content[:12_000]},
                {
                    "role": "user",
                    "content": (
                        "Repair the previous response. Return only one valid JSON object "
                        "matching the required schema."
                    ),
                },
            ]
            repaired = await provider.generate(profile, api_key, repair_messages)
            try:
                return StructuredRecommendation.model_validate_json(repaired.content), repaired
            except ValidationError as exc:
                raise ProviderError(
                    "provider output failed schema validation after one repair"
                ) from exc
