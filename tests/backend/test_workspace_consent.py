from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from neuroagent.agent.gateway import ModelGatewayError, ToolTurnResult
from neuroagent.agent.models import ModelCapability, ProviderResponse, ToolCall
from neuroagent.agent.providers import ProviderError, ProviderTimeoutError
from neuroagent.api import create_app
from neuroagent.application.contracts.agent import (
    ModelProfileCreate,
    ModelProfileInput,
)
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationMode,
    WorkspaceContentConsentCreate,
)
from neuroagent.application.errors import InputValidationError, NotFoundError
from neuroagent.application.services import ApplicationServices


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
    service: ApplicationServices,
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
    return service.models.create_model_profile(
        ModelProfileCreate(profile=profile), f"profile-write-{base_url}-{tool_use}"
    )


def add_conversation(service: ApplicationServices, workspace: Path) -> str:
    return service.conversations.create_conversation(
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
    service: ApplicationServices,
    gateway: ProbeGateway,
) -> None:
    monkeypatch.setattr(service.models, "_model_gateway", lambda: gateway)


def enable_workspace_assistant(service: ApplicationServices) -> None:
    service.settings = service.settings.model_copy(
        update={"redaction_salt": "synthetic-test-redaction-salt"}
    )


@pytest.mark.asyncio
async def test_workspace_consent_requires_declared_tool_use_without_calling_provider(
    service: ApplicationServices, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_workspace_assistant(service)
    add_profile(service, tool_use=False)
    conversation_id = add_conversation(service, source_root)
    gateway = ProbeGateway(successful_probe())
    install_gateway(monkeypatch, service, gateway)

    with pytest.raises(InputValidationError) as caught:
        await service.workspaces.create_workspace_content_consent(
            conversation_id,
            WorkspaceContentConsentCreate(
                profile_id="test-provider",
                model="chosen-concrete-model",
                acknowledge_remote_processing=True,
            ),
        )

    assert caught.value.code == "workspace_tool_use_required"
    assert gateway.calls == []
    assert service.workspaces.list_workspace_content_consents(conversation_id) == []


@pytest.mark.asyncio
async def test_workspace_consent_probes_exact_model_without_workspace_content(
    service: ApplicationServices, source_root: Path, monkeypatch: pytest.MonkeyPatch
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
    assert service.workspaces.workspace_content_consent_granted(
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
    service: ApplicationServices,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: ProviderResponse,
) -> None:
    enable_workspace_assistant(service)
    add_profile(service)
    conversation_id = add_conversation(service, source_root)
    install_gateway(monkeypatch, service, ProbeGateway(response))

    with pytest.raises(InputValidationError) as caught:
        await service.workspaces.create_workspace_content_consent(
            conversation_id,
            WorkspaceContentConsentCreate(
                profile_id="test-provider",
                model="chosen-concrete-model",
                acknowledge_remote_processing=True,
            ),
        )

    assert caught.value.code == "workspace_tool_probe_failed"
    assert service.workspaces.list_workspace_content_consents(conversation_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ModelGatewayError("unavailable"), ProviderError("rejected")])
async def test_workspace_consent_reports_provider_probe_failures(
    service: ApplicationServices,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    enable_workspace_assistant(service)
    add_profile(service)
    conversation_id = add_conversation(service, source_root)
    install_gateway(monkeypatch, service, ProbeGateway(failure))

    with pytest.raises(InputValidationError) as caught:
        await service.workspaces.create_workspace_content_consent(
            conversation_id,
            WorkspaceContentConsentCreate(
                profile_id="test-provider",
                model="chosen-concrete-model",
                acknowledge_remote_processing=True,
            ),
        )

    assert caught.value.code == "workspace_tool_probe_failed"
    assert service.workspaces.list_workspace_content_consents(conversation_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,reason",
    [
        (
            ProviderResponse(
                content="",
                model="chosen-concrete-model",
                finish_reason="length",
                usage={"completion_tokens": 2048},
            ),
            "output_truncated",
        ),
        (ProviderTimeoutError("synthetic deadline"), "timeout"),
    ],
)
async def test_probe_failure_diagnosis_does_not_claim_unsupported_tools(
    service, source_root, monkeypatch, response, reason
):
    enable_workspace_assistant(service)
    add_profile(service)
    conversation_id = add_conversation(service, source_root)
    install_gateway(monkeypatch, service, ProbeGateway(response))
    with pytest.raises(InputValidationError) as caught:
        await service.workspaces.create_workspace_content_consent(
            conversation_id,
            WorkspaceContentConsentCreate(
                profile_id="test-provider",
                model="chosen-concrete-model",
                acknowledge_remote_processing=True,
            ),
        )
    assert caught.value.details["reason"] == reason
    assert service.workspaces.list_workspace_content_consents(conversation_id) == []


@pytest.mark.asyncio
async def test_workspace_consent_is_invalidated_by_profile_revision_and_revocation(
    service: ApplicationServices, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_workspace_assistant(service)
    add_profile(service)
    conversation_id = add_conversation(service, source_root)
    gateway = ProbeGateway(successful_probe())
    install_gateway(monkeypatch, service, gateway)
    consent = await service.workspaces.create_workspace_content_consent(
        conversation_id,
        WorkspaceContentConsentCreate(
            profile_id="test-provider",
            model="chosen-concrete-model",
            acknowledge_remote_processing=True,
        ),
    )
    assert consent.valid_for_current_context is True

    add_profile(service, base_url="https://another-provider.example/v1")
    assert not service.workspaces.workspace_content_consent_granted(
        conversation_id, str(source_root), "test-provider", "chosen-concrete-model"
    )
    assert (
        service.workspaces.list_workspace_content_consents(conversation_id)[
            0
        ].valid_for_current_context
        is False
    )

    revoked = service.workspaces.revoke_workspace_content_consent(
        conversation_id, consent.consent_id
    )
    assert revoked.revoked_at is not None
    assert not service.workspaces.workspace_content_consent_granted(
        conversation_id, str(source_root), "test-provider", "chosen-concrete-model"
    )


def test_profile_revision_is_independent_of_capability_serialization_order(
    service: ApplicationServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = add_profile(service)
    expected = service.workspaces._profile_revision(view)
    model_type = type(view.profile)
    original = model_type.model_dump

    def reverse_capabilities(self, **kwargs):
        value = original(self, **kwargs)
        value["capabilities"] = list(reversed(value["capabilities"]))
        return value

    monkeypatch.setattr(model_type, "model_dump", reverse_capabilities)
    assert service.workspaces._profile_revision(view) == expected


@pytest.mark.asyncio
async def test_regrant_refreshes_revoked_workspace_consent_in_place(
    service: ApplicationServices, source_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enable_workspace_assistant(service)
    add_profile(service)
    conversation_id = add_conversation(service, source_root)
    install_gateway(monkeypatch, service, ProbeGateway(successful_probe()))
    request = WorkspaceContentConsentCreate(
        profile_id="test-provider",
        model="chosen-concrete-model",
        acknowledge_remote_processing=True,
    )

    first = await service.workspaces.create_workspace_content_consent(conversation_id, request)
    revoked = service.workspaces.revoke_workspace_content_consent(conversation_id, first.consent_id)
    renewed = await service.workspaces.create_workspace_content_consent(conversation_id, request)

    assert revoked.revoked_at is not None
    assert renewed.consent_id == first.consent_id
    assert renewed.revoked_at is None
    assert renewed.granted_at > first.granted_at
    assert renewed.valid_for_current_context is True
    assert len(service.workspaces.list_workspace_content_consents(conversation_id)) == 1


def test_workspace_consent_persistence_enforces_conversation_scope(
    service: ApplicationServices, source_root: Path
) -> None:
    conversation_id = add_conversation(service, source_root)
    other_conversation_id = service.conversations.create_conversation(
        ConversationCreate(mode=ConversationMode.WORK, workspace_path=str(source_root)),
        "other-workspace-consent-conversation",
    ).conversation_id
    consent = service.repository.create_workspace_consent(
        conversation_id=conversation_id,
        workspace_hash="a" * 64,
        profile_id="test-provider",
        profile_revision="b" * 64,
        model="chosen-model",
        policy_version="workspace-content-v1",
        provider_host="provider.example",
    )

    with pytest.raises(NotFoundError):
        service.repository.list_workspace_consents("missing-conversation")
    with pytest.raises(NotFoundError):
        service.repository.revoke_workspace_consent(other_conversation_id, consent.consent_id)
