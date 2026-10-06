"""Work tool loop: read-only catalog, JSON contract, and the fallback guarantee.

Two properties matter here.  First, the loop must not become a way for a model to
reach a server-only Work action.  Second, the loop is an enrichment: when it
produces nothing usable, Work must still answer through the single-shot JSON
path rather than degrade.
"""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from neuroagent.agent.gateway import ChatGatewayResult
from neuroagent.agent.models import ModelCapability, ModelProfile, ProviderResponse, ToolCall
from neuroagent.agent.providers import MockProvider, ProviderTimeoutError
from neuroagent.api.app import create_app
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service

_SALT = "stable-work-tool-loop-redaction-salt"
#: The Work read catalog, in the order the dispatcher advertises it.
_WORK_CATALOG = (
    "describe_statistical_capabilities",
    "get_run_status",
    "get_statistical_results",
    "list_run_artifacts",
    "list_skills",
)


def _profile(*capabilities: ModelCapability) -> ModelProfile:
    return ModelProfile(
        id="work-tool-profile",
        provider="openai-compatible",
        base_url="https://example.org/v1",
        model="work-tool-model",
        api_key_env="WORK_TOOL_API_KEY",
        capabilities=frozenset(capabilities),
    )


def _work_json(action: str = "conversation", answer: str = "已核对方法。") -> str:
    return json.dumps({"answer": answer, "action": action, "draft_patch": {}})


def _service(tmp_path: Path, source_root: Path, provider: MockProvider) -> Any:
    settings = Settings(
        rag_db_dir=None,
        database_url=f"sqlite:///{(tmp_path / 'work-tool.sqlite').as_posix()}",
        allowed_source_roots=[source_root],
        allowed_work_root=tmp_path / "work",
        redaction_salt=_SALT,
        secrets_file=tmp_path / ".env",
    )
    return build_service(settings, providers={"openai-compatible": provider})


def _turn(client: TestClient, service: Any, key: str) -> dict[str, Any]:
    conversation = client.post(
        "/api/v1/conversations",
        json={"mode": "work", "preferred_profile_id": "work-tool-profile"},
        headers={"Idempotency-Key": f"{key}-create"},
    ).json()
    response = client.post(
        f"/api/v1/conversations/{conversation['conversation_id']}/turns",
        json={
            "content": "这个方法用哪种多重比较校正?",
            "preferred_profile_id": "work-tool-profile",
        },
        headers={"Idempotency-Key": f"{key}-turn"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_work_catalog_is_read_only() -> None:
    from neuroagent.agent.tool_spec import ToolTier
    from neuroagent.application.work_tools import build_work_tool_bindings

    bindings = build_work_tool_bindings(get_run=lambda run_id: {"run_id": run_id})

    # Declared order is stable for review; the provider sees the sorted catalog.
    assert sorted(binding.tool.name for binding in bindings) == list(_WORK_CATALOG)
    assert all(binding.tool.tier is ToolTier.READ for binding in bindings)
    # A privileged tool must not be reachable through the Work catalog at all.
    assert all(binding.tool.model_visible for binding in bindings)


def test_work_catalog_handlers_project_only_aggregates() -> None:
    from neuroagent.application.work_tools import build_work_tool_bindings

    bindings = {
        binding.tool.name: binding.handler
        for binding in build_work_tool_bindings(
            get_run=lambda run_id: {
                "run_id": run_id,
                "state": "qc_review",
                "stage": "qc",
                "attempt": 1,
                "qc_status": "pending",
                "cancel_requested": False,
                "workspace_path": r"D:\private\work",
            },
            list_artifacts=lambda run_id: [
                {"artifact_type": "metric.alff"},
                {"artifact_type": "metric.alff"},
                {"artifact_type": "qc.head_motion_report"},
            ],
        )
    }

    status = dict(bindings["get_run_status"]({"run_id": "r1"}))
    assert status["state"] == "qc_review"
    # Raw workspace paths must not be forwarded to the model.
    assert "workspace_path" not in status

    artifacts = dict(bindings["list_run_artifacts"]({"run_id": "r1"}))
    assert artifacts["total"] == 3
    assert {"artifact_type": "metric.alff", "count": 2} in artifacts["artifact_types"]


def test_work_catalog_hides_runs_outside_request_scope() -> None:
    from neuroagent.application.work_tools import build_work_tool_bindings

    calls: list[str] = []

    def get_run(run_id: str) -> dict[str, Any]:
        calls.append(run_id)
        return {"run_id": run_id, "project_id": "project-a", "state": "running"}

    bindings = {
        binding.tool.name: binding.handler
        for binding in build_work_tool_bindings(
            get_run=get_run,
            list_artifacts=lambda run_id: [{"artifact_type": "secret"}],
            project_id="project-a",
            allowed_run_ids=frozenset({"run-a"}),
        )
    }

    hidden = dict(bindings["get_run_status"]({"run_id": "run-b"}))
    assert hidden == {"run_id": "run-b", "status": "not_available_in_scope"}
    assert dict(bindings["list_run_artifacts"]({"run_id": "run-b"}))["total"] == 0
    assert calls == []

    visible = dict(bindings["get_run_status"]({"run_id": "run-a"}))
    assert visible["state"] == "running"
    assert calls == ["run-a"]


def test_work_tool_loop_answers_in_the_json_contract(
    tmp_path: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORK_TOOL_API_KEY", "test-secret")
    provider = MockProvider(
        [
            ProviderResponse(
                content="",
                model="work-tool-model",
                tool_calls=(
                    ToolCall(
                        call_id="c1",
                        name="describe_statistical_capabilities",
                        arguments={},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ProviderResponse(
                content=_work_json(answer="该安装支持 FDR 与 GRF。"), model="work-tool-model"
            ),
        ]
    )
    service = _service(tmp_path, source_root, provider)
    single_shot = AsyncMock(side_effect=AssertionError("loop answered; single shot must not run"))
    monkeypatch.setattr(service, "_generate_rsfmri_chat", single_shot)
    try:
        service.repository.create_model_profile(
            _profile(ModelCapability.JSON_OBJECT, ModelCapability.TOOL_USE)
        )
        with TestClient(create_app(service=service)) as client:
            body = _turn(client, service, "work-loop")
    finally:
        service.close()

    single_shot.assert_not_called()
    # The read catalog was offered, and the tool-gathering turn carried no JSON mode.
    assert provider.tool_requests[0] == tuple(sorted((*_WORK_CATALOG, "get_run_diagnosis")))

    payload = body["assistant_message"]["payload"]
    loop = payload["tool_loop"]
    assert loop["enabled"] is True
    assert loop["stop_reason"] == "completed"
    assert loop["tool_names"] == ["describe_statistical_capabilities"]
    assert body["assistant_message"]["content"] == "该安装支持 FDR 与 GRF。"

    # Loop steps are audited like any other tool call.
    steps = body["tool_calls"]
    assert [step["tool_name"] for step in steps] == ["describe_statistical_capabilities"]
    assert steps[0]["risk_tier"] == "read"
    # The live tool ran against the real service, not a stub.
    assert steps[0]["status"] == "succeeded"


def test_work_loop_cannot_reach_a_server_only_action(
    tmp_path: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool-capable model gets no new authority over Work actions."""

    monkeypatch.setenv("WORK_TOOL_API_KEY", "test-secret")
    provider = MockProvider(
        [
            ProviderResponse(
                content=_work_json(action="start_preprocessing"), model="work-tool-model"
            )
        ]
    )
    service = _service(tmp_path, source_root, provider)
    execute = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(service.conversation_work, "execute", execute)
    try:
        service.repository.create_model_profile(
            _profile(ModelCapability.JSON_OBJECT, ModelCapability.TOOL_USE)
        )
        with TestClient(create_app(service=service)) as client:
            body = _turn(client, service, "work-loop-privileged")
    finally:
        service.close()

    execute.assert_not_called()
    assert "未执行任务" in body["assistant_message"]["content"]


def test_work_loop_falls_back_when_the_answer_is_not_json(
    tmp_path: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-JSON loop answer must not degrade Work; the JSON path retries."""

    monkeypatch.setenv("WORK_TOOL_API_KEY", "test-secret")
    provider = MockProvider(
        [ProviderResponse(content="我不确定，这取决于你的设计。", model="work-tool-model")]
    )
    service = _service(tmp_path, source_root, provider)
    fallback = AsyncMock(
        return_value=ChatGatewayResult(
            response=ProviderResponse(
                content=_work_json(answer="回退回答。"), model="work-tool-model"
            ),
            selected_profile_id="work-tool-profile",
            context_hash="b" * 64,
            attempted_profile_ids=("work-tool-profile",),
            remote_search_used=False,
            redaction_count=0,
        )
    )
    monkeypatch.setattr(service, "_generate_rsfmri_chat", fallback)
    try:
        service.repository.create_model_profile(
            _profile(ModelCapability.JSON_OBJECT, ModelCapability.TOOL_USE)
        )
        with TestClient(create_app(service=service)) as client:
            body = _turn(client, service, "work-loop-fallback")
    finally:
        service.close()

    assert fallback.await_count == 1
    assert body["assistant_message"]["content"] == "回退回答。"
    # The fallback is single-shot, but the attempted loop remains auditable.
    assert body["assistant_message"]["payload"]["tool_loop"]["fallback_candidate"] is False
    assert body["assistant_message"]["payload"]["tool_loop"]["calls"] == []


def test_work_timeout_preserves_draft_without_repeating_provider_request(
    tmp_path, source_root, monkeypatch
):
    monkeypatch.setenv("WORK_TOOL_API_KEY", "test-secret")
    provider = MockProvider([ProviderTimeoutError("synthetic timeout")])
    service = _service(tmp_path, source_root, provider)
    fallback = AsyncMock(side_effect=AssertionError("timeout must not trigger a second request"))
    monkeypatch.setattr(service, "_generate_rsfmri_chat", fallback)
    try:
        service.repository.create_model_profile(
            _profile(ModelCapability.JSON_OBJECT, ModelCapability.TOOL_USE)
        )
        with TestClient(create_app(service=service)) as client:
            result = _turn(client, service, "work-loop-timeout")
    finally:
        service.close()
    fallback.assert_not_called()
    assert len(provider.requests) == 1
    assert result["assistant_message"]["payload"]["model_error"]["provider_timeout"] is True
    assert "未执行任务" in result["assistant_message"]["content"]


def test_work_uses_single_shot_when_the_profile_lacks_tool_use(
    tmp_path: Path,
    source_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORK_TOOL_API_KEY", "test-secret")
    provider = MockProvider([ProviderResponse(content="unused", model="work-tool-model")])
    service = _service(tmp_path, source_root, provider)
    single_shot = AsyncMock(
        return_value=ChatGatewayResult(
            response=ProviderResponse(
                content=_work_json(answer="单次回答。"), model="work-tool-model"
            ),
            selected_profile_id="work-tool-profile",
            context_hash="c" * 64,
            attempted_profile_ids=("work-tool-profile",),
            remote_search_used=False,
            redaction_count=0,
        )
    )
    monkeypatch.setattr(service, "_generate_rsfmri_chat", single_shot)
    try:
        service.repository.create_model_profile(_profile(ModelCapability.JSON_OBJECT))
        with TestClient(create_app(service=service)) as client:
            body = _turn(client, service, "work-no-tools")
    finally:
        service.close()

    assert single_shot.await_count == 1
    assert body["assistant_message"]["content"] == "单次回答。"
    assert "tool_loop" not in body["assistant_message"]["payload"]
    # A profile without tool_use must never be offered tools.
    assert all(not entry for entry in provider.tool_requests)
