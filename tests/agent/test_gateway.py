import json

import pytest
from pydantic import ValidationError

from neuroagent.agent.gateway import ModelGateway, ModelGatewayError
from neuroagent.agent.models import (
    AgentSummaryPurpose,
    AgentTaskRequest,
    ModelCapability,
    ModelProfile,
    ProviderResponse,
    TaskType,
)
from neuroagent.agent.providers import MockProvider, RetryableProviderError
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.agent.router import ModelRouter
from neuroagent.agent.secrets import ProcessEnvironmentSecretResolver
from neuroagent.infrastructure.secrets import LocalDotenvSecretResolver


def profile(profile_id: str, key: str, priority: int) -> ModelProfile:
    return ModelProfile(
        id=profile_id,
        provider=profile_id,
        base_url="https://provider.example/v1",
        model="test-model",
        api_key_env=key,
        priority=priority,
        capabilities=frozenset({ModelCapability.JSON_OBJECT}),
    )


@pytest.mark.parametrize(
    "base_url",
    (
        "http://localhost.evil.example/v1",
        "http://127.0.0.1.attacker.example/v1",
        "http://127.0.0.1:invalid/v1",
    ),
)
def test_model_profile_rejects_ambiguous_http_hosts(base_url: str) -> None:
    with pytest.raises(ValidationError, match="base_url"):
        ModelProfile(
            id="unsafe-profile",
            provider="unsafe-provider",
            base_url=base_url,
            model="test-model",
            api_key_env="UNSAFE_PROVIDER_API_KEY",
        )


@pytest.mark.parametrize(
    "base_url",
    ("http://localhost:8080/v1", "http://127.0.0.2/v1", "http://[::1]:8000/v1"),
)
def test_model_profile_accepts_exact_http_loopback_hosts(base_url: str) -> None:
    assert (
        ModelProfile(
            id="local-profile",
            provider="local-provider",
            base_url=base_url,
            model="test-model",
            api_key_env="LOCAL_PROVIDER_API_KEY",
        ).base_url
        == base_url
    )


@pytest.mark.asyncio
async def test_retryable_failure_uses_declared_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    first = profile("first-provider", "FIRST_KEY", 1)
    second = profile("second-provider", "SECOND_KEY", 2)
    router = ModelRouter(
        [first, second],
        {TaskType.SKILL_PLANNER.value: [first.id, second.id]},
    )
    providers = {
        first.provider: MockProvider([RetryableProviderError("rate limited")]),
        second.provider: MockProvider(
            [
                json.dumps(
                    {
                        "summary": "A reviewable plan is ready.",
                        "proposed_skill_request": None,
                        "warnings": [],
                        "unresolved_questions": ["Confirm the frequency band."],
                        "requires_user_confirmation": True,
                    }
                )
            ]
        ),
    }
    monkeypatch.setenv("FIRST_KEY", "test-secret-one")
    monkeypatch.setenv("SECOND_KEY", "test-secret-two")
    gateway = ModelGateway(
        router,
        providers,
        OutboundContextPolicy("a-stable-test-salt-value"),
        ProcessEnvironmentSecretResolver(),
    )

    result = await gateway.generate(
        AgentTaskRequest(
            task_type=TaskType.SKILL_PLANNER,
            project_id="project-1",
            summary={"purpose": AgentSummaryPurpose.EXPLAIN_CURRENT_PLAN},
        )
    )

    assert result.routing.selected_profile_id == second.id
    assert result.attempted_profile_ids == (first.id, second.id)
    assert result.recommendation.requires_user_confirmation is True


@pytest.mark.asyncio
async def test_schema_failure_gets_one_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = profile("only-provider", "ONLY_KEY", 1)
    provider = MockProvider(
        [
            "not-json",
            json.dumps(
                {
                    "summary": "Repaired.",
                    "proposed_skill_request": None,
                    "warnings": [],
                    "unresolved_questions": [],
                    "requires_user_confirmation": True,
                }
            ),
        ]
    )
    monkeypatch.setenv("ONLY_KEY", "test-secret")
    gateway = ModelGateway(
        ModelRouter([configured], {}),
        {configured.provider: provider},
        OutboundContextPolicy("a-stable-test-salt-value"),
        ProcessEnvironmentSecretResolver(),
    )

    result = await gateway.generate(
        AgentTaskRequest(
            task_type=TaskType.REPORT_WRITER,
            project_id="project-1",
            summary={"purpose": AgentSummaryPurpose.DRAFT_METHOD_REPORT},
        )
    )

    assert result.recommendation.summary == "Repaired."
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_missing_keys_fail_without_sending() -> None:
    configured = profile("only-provider", "UNSET_TEST_KEY", 1)
    provider = MockProvider([])
    gateway = ModelGateway(
        ModelRouter([configured], {}),
        {configured.provider: provider},
        OutboundContextPolicy("a-stable-test-salt-value"),
        ProcessEnvironmentSecretResolver(),
    )

    with pytest.raises(ModelGatewayError):
        await gateway.generate(
            AgentTaskRequest(
                task_type=TaskType.REPORT_WRITER,
                project_id="project-1",
                summary={"purpose": AgentSummaryPurpose.DRAFT_METHOD_REPORT},
            )
        )
    assert provider.requests == []


@pytest.mark.asyncio
async def test_local_dotenv_secret_reaches_provider_with_process_precedence(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CapturingProvider:
        def __init__(self) -> None:
            self.keys: list[str] = []

        async def generate(self, configured, api_key, messages):  # type: ignore[no-untyped-def]
            del configured, messages
            self.keys.append(api_key)
            return ProviderResponse(
                content=json.dumps(
                    {
                        "summary": "Connected.",
                        "proposed_skill_request": None,
                        "warnings": [],
                        "unresolved_questions": [],
                        "requires_user_confirmation": True,
                    }
                ),
                model="test-model",
            )

    env_file = tmp_path / ".env"
    env_file.write_text("DOTENV_PROVIDER_API_KEY=file-secret\n", encoding="utf-8")
    configured = profile("dotenv-provider", "DOTENV_PROVIDER_API_KEY", 1)
    provider = CapturingProvider()
    resolver = LocalDotenvSecretResolver(env_file)

    monkeypatch.setenv("DOTENV_PROVIDER_API_KEY", "process-secret")
    first_gateway = ModelGateway(
        ModelRouter([configured], {}),
        {configured.provider: provider},
        OutboundContextPolicy("a-stable-test-salt-value"),
        resolver,
    )
    request = AgentTaskRequest(
        task_type=TaskType.REPORT_WRITER,
        project_id="project-1",
        summary={"purpose": AgentSummaryPurpose.DRAFT_METHOD_REPORT},
    )
    await first_gateway.generate(request)

    monkeypatch.delenv("DOTENV_PROVIDER_API_KEY")
    second_gateway = ModelGateway(
        ModelRouter([configured], {}),
        {configured.provider: provider},
        OutboundContextPolicy("a-stable-test-salt-value"),
        resolver,
    )
    await second_gateway.generate(request)

    assert provider.keys == ["process-secret", "file-secret"]


def test_arbitrary_or_clinical_context_is_rejected_before_provider_call() -> None:
    provider = MockProvider([])

    with pytest.raises(ValidationError):
        AgentTaskRequest(
            task_type=TaskType.REPORT_WRITER,
            project_id="project-1",
            summary={"user_question": "请分析张三的 ALFF 结果", "ages": [31]},
        )
    assert provider.requests == []
