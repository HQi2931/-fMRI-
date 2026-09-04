"""Model routing and Agent task use cases."""

from __future__ import annotations

from pydantic import ValidationError

from neuroagent.agent.gateway import ModelGateway, ModelGatewayError
from neuroagent.agent.models import (
    AgentSummaryPurpose,
    AgentTaskRequest,
    GatewayResult,
    SafeAgentSummary,
    TaskType,
)
from neuroagent.agent.providers import OpenAICompatibleProvider, ProviderError
from neuroagent.agent.redaction import OutboundContextPolicy, OutboundPolicyError
from neuroagent.agent.router import ModelRouter, ModelRoutingError
from neuroagent.application.contracts import (
    AgentTaskCreate,
    AgentTaskView,
    ModelListRequest,
    ModelListView,
    ModelProfileCreate,
    ModelProfileView,
    ProviderTestRequest,
    ProviderTestView,
)
from neuroagent.application.errors import ApplicationError, ConflictError, InputValidationError
from neuroagent.application.service_mixins._base import BaseServiceMixin
from neuroagent.skills.models import SkillRequest


class ModelAgentMixin(BaseServiceMixin):
    def create_model_profile(
        self, request: ModelProfileCreate, idempotency_key: str
    ) -> ModelProfileView:
        def action() -> ModelProfileView:
            if request.api_key:
                self.secret_writer.write(
                    self.settings.secrets_file,
                    request.profile.api_key_env,
                    request.api_key,
                )
            result = self.repository.create_model_profile(request.profile)
            self.repository.append_event(
                project_id=None,
                run_id=None,
                event_type="ModelProfileCreated",
                severity="info",
                payload={
                    "profile_id": request.profile.id,
                    "provider": request.profile.provider,
                    "api_key_env": request.profile.api_key_env,
                },
            )
            return result

        return self._idempotent(
            scope="model-profiles:create",
            key=idempotency_key,
            request=request,
            response_type=ModelProfileView,
            action=action,
        )

    def list_model_profiles(self) -> list[ModelProfileView]:
        return self.repository.list_model_profiles()

    def get_model_profile(self, profile_id: str) -> ModelProfileView:
        return self.repository.get_model_profile(profile_id)

    def delete_model_profile(self, profile_id: str) -> None:
        self.repository.delete_model_profile(profile_id)
        self.repository.append_event(
            project_id=None,
            run_id=None,
            event_type="ModelProfileDeleted",
            severity="info",
            payload={"profile_id": profile_id},
        )

    async def list_provider_models(self, request: ModelListRequest) -> ModelListView:
        api_key = (request.api_key or "").strip() or None
        if api_key is None and request.api_key_env:
            api_key = self.secret_resolver.resolve(request.api_key_env)
        if not api_key:
            raise InputValidationError(
                "model_api_key_missing",
                "未提供 API Key 或密钥环境变量; 请在前端填写 API Key 或配置本地 .env。",
            )
        provider = self.providers.get("openai-compatible")
        if not isinstance(provider, OpenAICompatibleProvider):
            raise ApplicationError(
                "model_list_unavailable",
                "当前没有可用的 OpenAI-compatible 模型适配器。",
                status_code=503,
            )
        try:
            models = await provider.list_models(request.base_url, api_key)
        except ProviderError as exc:
            raise ApplicationError(
                "model_list_unavailable",
                "无法获取模型列表或返回内容无效。",
                status_code=503,
            ) from exc
        return ModelListView(models=models)

    def _model_gateway(self) -> ModelGateway:
        if self.settings.redaction_salt is None:
            raise InputValidationError(
                "redaction_policy_not_configured",
                "未配置 RSFMRI_REDACTION_SALT, 外部模型调用已关闭。",
            )
        try:
            policy = OutboundContextPolicy(self.settings.redaction_salt)
        except OutboundPolicyError as exc:
            raise InputValidationError(
                "redaction_policy_invalid",
                "脱敏策略配置无效, 外部模型调用已关闭。",
            ) from exc
        profiles = [view.profile for view in self.repository.list_model_profiles()]
        return ModelGateway(
            ModelRouter(profiles, {}),
            self.providers,
            policy,
            self.secret_resolver,
        )

    async def _generate_recommendation(self, request: AgentTaskRequest) -> GatewayResult:
        try:
            result = await self._model_gateway().generate(request)
        except OutboundPolicyError as exc:
            raise InputValidationError(
                "outbound_context_rejected",
                "外发上下文无法确认已安全脱敏, 模型调用已阻断。",
            ) from exc
        except ModelRoutingError as exc:
            raise InputValidationError(
                "model_route_unavailable",
                "没有满足任务能力要求的模型配置。",
            ) from exc
        except (ModelGatewayError, ProviderError) as exc:
            raise ApplicationError(
                "model_gateway_unavailable",
                "模型服务不可用或返回内容未通过结构校验。",
                status_code=503,
            ) from exc
        proposed = result.recommendation.proposed_skill_request
        if proposed is not None:
            try:
                SkillRequest.model_validate(proposed)
            except ValidationError as exc:
                raise InputValidationError(
                    "agent_skill_request_invalid",
                    "模型提出的 SkillRequest 未通过严格结构和科研参数校验。",
                ) from exc
        return result

    async def test_provider(
        self, request: ProviderTestRequest, idempotency_key: str
    ) -> ProviderTestView:
        async def prepare() -> GatewayResult:
            profile = self.repository.get_model_profile(request.profile_id)
            if profile.version != request.expected_profile_version:
                raise ConflictError(
                    "revision_conflict",
                    "模型配置版本已变化, 请刷新后重试。",
                    expected=request.expected_profile_version,
                    actual=profile.version,
                )
            return await self._generate_recommendation(
                AgentTaskRequest(
                    task_type=TaskType.PLAN_EXPLAINER,
                    project_id="provider-connectivity-test",
                    summary=SafeAgentSummary(
                        purpose=AgentSummaryPurpose.PROVIDER_CONNECTIVITY_TEST
                    ),
                    preferred_profile_id=request.profile_id,
                )
            )

        def finalize(result: GatewayResult) -> ProviderTestView:
            return ProviderTestView(
                profile_id=request.profile_id,
                available=True,
                routing=result.routing,
                context_hash=result.context_hash,
            )

        return await self._idempotent_async(
            scope=f"model-profiles:{request.profile_id}:test",
            key=idempotency_key,
            request=request,
            response_type=ProviderTestView,
            prepare=prepare,
            finalize=finalize,
        )

    async def create_agent_task(
        self, request: AgentTaskCreate, idempotency_key: str
    ) -> AgentTaskView:
        async def prepare() -> GatewayResult:
            self._require_project_version(
                request.request.project_id, request.expected_project_version
            )
            return await self._generate_recommendation(request.request)

        def finalize(result: GatewayResult) -> AgentTaskView:
            task = self.repository.create_agent_task(
                project_id=request.request.project_id,
                expected_project_version=request.expected_project_version,
                task_type=request.request.task_type.value,
                result=result,
            )
            self.repository.append_event(
                project_id=task.project_id,
                run_id=None,
                event_type="AgentTaskCompleted",
                severity="info",
                payload={
                    "task_id": task.task_id,
                    "task_type": request.request.task_type.value,
                    "context_hash": result.context_hash,
                    "selected_profile_id": result.routing.selected_profile_id,
                },
            )
            return task

        return await self._idempotent_async(
            scope=f"projects:{request.request.project_id}:agent-tasks:create",
            key=idempotency_key,
            request=request,
            response_type=AgentTaskView,
            prepare=prepare,
            finalize=finalize,
        )

    def get_agent_task(self, task_id: str) -> AgentTaskView:
        return self.repository.get_agent_task(task_id)
