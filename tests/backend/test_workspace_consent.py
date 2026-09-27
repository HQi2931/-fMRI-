from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from neuroagent.agent.gateway import ModelGatewayError, ToolTurnResult
from neuroagent.agent.models import ModelCapability, ProviderResponse, ToolCall
from neuroagent.agent.providers import ProviderError
from neuroagent.api import create_app
from neuroagent.application.contracts import (
    ConversationCreate,
    ConversationMode,
    ModelProfileCreate,
    ModelProfileInput,
    WorkspaceContentConsentCreate,
)
from neuroagent.application.errors import InputValidationError
from neuroagent.application.services import NeuroAgentService


class ProbeGateway:
    def __init__(self, response: ProviderResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def generate_tool_turn(self, **kwargs: object) -> ToolTurnResult:
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return ToolTurnResult(
            response=self.response,
            selected_profile_id=str(kwargs["preferred_profile_id"]),
            attempted_profile_ids=(str(kwargs["preferred_profile_id"]),),
        )


def add_profile(
    service: NeuroAgentService,
    *,
    tool_use: bool = True,
    base_url: str = "https://provider.example/v1",
):
    capabilities = {ModelCapability.JSON_OBJECT}
    if tool_use:
        capabilities.add(ModelCapability.TOOL_USE)
    profile = ModelProfileInput(
        id="test-provider",
        provider="openai-compatible",
        base_url=base_url,
        model="configured-model",
        api_key_env="TEST_PROVIDER_API_KEY",
        capabilities=frozenset(capabilities),
    )
    return service.create_model_profile(
        ModelProfileCreate(profile=profile), f"profile-write-{base_url}-{tool_use}"
    )


def add_conversation(service: NeuroAgentService, workspace: Path) -> str:
    return service.create_conversation(
        ConversationCreate(mode=ConversationMode.WORK, workspace_path=str(workspace)),
        "workspace-consent-conversation",
    ).conversation_id


def successful_probe() -> ProviderResponse:
    return ProviderResponse(
        content="",
        model="chosen-concrete-model",
        tool_calls=(ToolCall(call_id="probe-1", name="workspace_tool_probe", arguments={}),),
    )


def install_gateway(
    monkeypatch: pytest.MonkeyPatch,
    service: NeuroAgentService,
    gateway: ProbeGateway,
) -> None:
    monkeypatch.setattr(service, "_model_gateway", lambda: gateway)


def enable_workspace_assistant(service: NeuroAgentService) -> None:
    service.settings = service.settings.model_copy(
        update={"redaction_salt": "synthetic-test-redaction-salt"}
    )


@pytest.mark.asyncio
async def test_workspace_consent_requires_declared_tool_use_without_calling_provider(
    service: NeuroAgentService, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_workspace_assistant(service)
    add_profile(service, tool_use=False)
    conversation_id = add_conversation(service, source_root)
    gateway = ProbeGateway(successful_probe())
    install_gateway(monkeypatch, service, gateway)

    with pytest.raises(InputValidationError) as caught:
        await service.create_workspace_content_consent(
            conversation_id,
            WorkspaceContentConsentCreate(
                profile_id="test-provider",
                model="chosen-concrete-model",
                acknowledge_remote_processing=True,
            ),
        )

    assert caught.value.code == "workspace_tool_use_required"
    assert gateway.calls == []
    assert service.list_workspace_content_consents(conversation_id) == []


@pytest.mark.asyncio
async def test_workspace_consent_probes_exact_model_without_workspace_content(
    service: NeuroAgentService, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_workspace_assistant(service)
    add_profile(service)
    conversation_id = add_conversation(service, source_root)
    (source_root / "notes.txt").write_text("private-workspace-body", encoding="utf-8")
    gateway = ProbeGateway(successful_probe())
    install_gateway(monkeypatch, service, gateway)

    with TestClient(create_app(service=service)) as client:
        response = client.post(
            f"/api/v1/conversations/{conversation_id}/workspace/consents",
            json={
                "profile_id": "test-provider",
                "model": "chosen-concrete-model",
                "acknowledge_remote_processing": True,
            },
        )

    assert response.status_code == 201
    consent = response.json()
    assert consent["valid_for_current_context"] is True
    assert consent["model"] == "chosen-concrete-model"
    assert consent["profile_revision"]
    assert len(gateway.calls) == 1
    call = gateway.calls[0]
    assert call["preferred_profile_id"] == "test-provider"
    assert call["model"] == "chosen-concrete-model"
    serialized_messages = json.dumps(call["messages"])
    assert str(source_root) not in serialized_messages
    assert "private-workspace-body" not in serialized_messages
    assert [tool.name for tool in call["tools"]] == ["workspace_tool_probe"]
    assert service.workspace_content_consent_granted(
        conversation_id, str(source_root), "test-provider", "chosen-concrete-model"
    )
    grant_event = next(
        event
        for event in service.repository.list_events(after_event_id=0)
        if event.event_type == "WorkspaceContentConsentGranted"
    )
    audit_payload = json.dumps(grant_event.payload)
    assert str(source_root) not in audit_payload
    assert "private-workspace-body" not in audit_payload
    assert "tool_probe" in audit_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        ProviderResponse(content="I can help.", model="chosen-concrete-model"),
        ProviderResponse(
            content="",
            model="chosen-concrete-model",
            tool_calls=(ToolCall(call_id="wrong", name="another_tool", arguments={}),),
        ),
        ProviderResponse(
            content="",
            model="chosen-concrete-model",
            tool_calls=(
                ToolCall(
                    call_id="invalid",
                    name="workspace_tool_probe",
                    arguments={},
                    arguments_valid=False,
                ),
            ),
        ),
    ],
)
async def test_workspace_consent_rejects_invalid_probe_results(
    service: NeuroAgentService,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: ProviderResponse,
) -> None:
    enable_workspace_assistant(service)
    add_profile(service)
    conversation_id = add_conversation(service, source_root)
    install_gateway(monkeypatch, service, ProbeGateway(response))

    with pytest.raises(InputValidationError) as caught:
        await service.create_workspace_content_consent(
            conversation_id,
            WorkspaceContentConsentCreate(
                profile_id="test-provider",
                model="chosen-concrete-model",
                acknowledge_remote_processing=True,
            ),
        )

    assert caught.value.code == "workspace_tool_probe_failed"
    assert service.list_workspace_content_consents(conversation_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ModelGatewayError("unavailable"), ProviderError("rejected")])
async def test_workspace_consent_reports_provider_probe_failures(
    service: NeuroAgentService,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    enable_workspace_assistant(service)
    add_profile(service)
    conversation_id = add_conversation(service, source_root)
    install_gateway(monkeypatch, service, ProbeGateway(failure))

    with pytest.raises(InputValidationError) as caught:
        await service.create_workspace_content_consent(
            conversation_id,
            WorkspaceContentConsentCreate(
                profile_id="test-provider",
                model="chosen-concrete-model",
                acknowledge_remote_processing=True,
            ),
        )

    assert caught.value.code == "workspace_tool_probe_failed"
    assert service.list_workspace_content_consents(conversation_id) == []


@pytest.mark.asyncio
async def test_workspace_consent_is_invalidated_by_profile_revision_and_revocation(
    service: NeuroAgentService, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_workspace_assistant(service)
    add_profile(service)
    conversation_id = add_conversation(service, source_root)
    gateway = ProbeGateway(successful_probe())
    install_gateway(monkeypatch, service, gateway)
    consent = await service.create_workspace_content_consent(
        conversation_id,
        WorkspaceContentConsentCreate(
            profile_id="test-provider",
            model="chosen-concrete-model",
            acknowledge_remote_processing=True,
        ),
    )
    assert consent.valid_for_current_context is True

    add_profile(service, base_url="https://another-provider.example/v1")
    assert not service.workspace_content_consent_granted(
        conversation_id, str(source_root), "test-provider", "chosen-concrete-model"
    )
    assert (
        service.list_workspace_content_consents(conversation_id)[0].valid_for_current_context
        is False
    )

    revoked = service.revoke_workspace_content_consent(conversation_id, consent.consent_id)
    assert revoked.revoked_at is not None
    assert not service.workspace_content_consent_granted(
        conversation_id, str(source_root), "test-provider", "chosen-concrete-model"
    )
