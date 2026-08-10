"""Redacted, schema-checked access to configured model providers."""

from __future__ import annotations

import json
from collections.abc import Mapping

from pydantic import ValidationError

from neuroagent.agent.models import (
    AgentTaskRequest,
    GatewayResult,
    ModelProfile,
    StructuredRecommendation,
)
from neuroagent.agent.providers import ModelProvider, ProviderError, RetryableProviderError
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.router import ModelRouter
from neuroagent.agent.secrets import SecretResolver


class ModelGatewayError(RuntimeError):
    pass


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
