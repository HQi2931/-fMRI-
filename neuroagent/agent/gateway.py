"""Redacted, schema-checked access to configured model providers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from neuroagent.agent.models import (
    AgentTaskRequest,
    GatewayResult,
    ModelProfile,
    ProviderResponse,
    StructuredRecommendation,
)
from neuroagent.agent.providers import ModelProvider, ProviderError, RetryableProviderError
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.router import ModelRouter
from neuroagent.agent.secrets import SecretResolver


class ModelGatewayError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatGatewayResult:
    response: ProviderResponse
    selected_profile_id: str
    context_hash: str
    attempted_profile_ids: tuple[str, ...]
    remote_search_used: bool


class ModelGateway:
    _system_prompt = (
        "You assist a research workflow. Return one JSON object with exactly these fields: "
        "summary (string), proposed_skill_request (object or null), warnings (string array), "
        "unresolved_questions (string array), requires_user_confirmation (boolean). "
        "Never emit commands, paths, tool calls, workflow transitions, or invented "
        "scientific defaults."
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
                recommendation = await self._request_structured(
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
            )
        if last_retryable:
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
        recent_messages: list[dict[str, str]] | None = None,
        pinned_context: list[dict[str, object]] | None = None,
        conversation_summary: str | None = None,
        routing: bool = False,
    ) -> ChatGatewayResult:
        context = self._outbound_policy.redact(
            {
                "question": question,
                "local_evidence": evidence,
                "recent_messages": recent_messages or [],
                "pinned_context": pinned_context or [],
                "conversation_summary": conversation_summary,
            }
        )
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

        system_prompt = (
            "你是 rs-fMRI 科研文字分析助手。只回答 rs-fMRI、DPABI、SPM、"
            "影像统计设计和相关参数问题。优先使用给出的本地证据; "
            "证据不足时明确说明不确定性, 不得编造科研结论、参数默认值或引用。"
            "回答使用中文, 并区分通用方法信息与用户项目事实。"
        )
        system_prompt += (
            "允许简短回应寒暄。历史消息用于理解追问, 不视为文献证据。"
            "引用本地证据时必须使用其 citation_id, 例如 [C1]; 只引用实际支持结论的片段。"
            "无本地证据时说明未找到文献依据, 一般解释不得冒充文献结论。"
        )
        if routing:
            system_prompt = (
                "根据当前问题与历史判断意图, 并将科研追问改写为独立检索问题。只返回 JSON: "
                '{"intent":"knowledge_query|conversation|work_request","query":"独立检索问题",'
                '"work_request":null}。执行需求的 work_request 为 {"task":"任务名",'
                '"parameters":{}}。只生成草案; 方法咨询不是执行请求。'
                "不要回答问题, 不要编造历史未提供的信息。"
            )
        if allow_web_search:
            system_prompt += (
                "本次允许使用联网搜索。仅引用与问题直接相关的公开来源, 并在结论旁保留来源引用。"
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(context.payload, ensure_ascii=False, sort_keys=True),
            },
        ]
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
                response = await provider.generate(
                    profile,
                    api_key,
                    messages,
                    web_search=allow_web_search,
                    json_object=routing,
                )
            except RetryableProviderError as exc:
                last_retryable = exc
                continue
            return ChatGatewayResult(
                response=response,
                selected_profile_id=profile.id,
                context_hash=context.context_hash,
                attempted_profile_ids=tuple(attempted),
                remote_search_used=allow_web_search,
            )
        if last_retryable:
            raise ModelGatewayError(
                "all available chat providers were temporarily unavailable"
            ) from last_retryable
        raise ModelGatewayError(
            "no routed chat provider has both an adapter and a configured API key"
        )

    async def _request_structured(
        self,
        provider: ModelProvider,
        profile: ModelProfile,
        api_key: str,
        payload: Mapping[str, object],
    ) -> StructuredRecommendation:
        messages = [
            {"role": "system", "content": self._system_prompt},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            },
        ]
        response = await provider.generate(profile, api_key, messages)
        try:
            return StructuredRecommendation.model_validate_json(response.content)
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
                return StructuredRecommendation.model_validate_json(repaired.content)
            except ValidationError as exc:
                raise ProviderError(
                    "provider output failed schema validation after one repair"
                ) from exc
