from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from neuroagent.agent.models import (
    ModelCapability,
    ModelProfile,
    ProviderCitation,
    ProviderResponse,
)
from neuroagent.agent.providers import MockProvider
from neuroagent.api import create_app
from neuroagent.application.contracts import RunCreate
from neuroagent.application.services import NeuroAgentService
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service, build_worker

from .conftest import make_approved_plan, make_project


def test_health_openapi_and_error_envelope(service: NeuroAgentService) -> None:
    with TestClient(create_app(service=service)) as client:
        assert client.get("/api/v1/health").json() == {"status": "ok", "database": "ok"}
        schema = client.get("/api/v1/openapi.json").json()
        assert "/api/v1/runs/{run_id}/events" in schema["paths"]
        contracts = schema["components"]["schemas"]
        assert "environment" not in contracts["SkillPlanResolveRequest"]["properties"]
        assert "input_artifact" not in contracts["SkillPlanIntent"]["properties"]
        assert "base_cfg_artifact_id" not in contracts["SkillPlanIntent"]["properties"]
        assert "environment_hash" not in contracts["StatisticalDesignCreate"]["properties"]
        response = client.get("/api/v1/projects/not-found")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
        assert response.json()["error"]["trace_id"]


def test_workspace_check_reports_dpabi_input_and_outputs(
    service: NeuroAgentService, source_root: Path
) -> None:
    (source_root / "FunRaw" / "S001").mkdir(parents=True)
    (source_root / "T1Raw" / "S001").mkdir(parents=True)
    nifti_header = (348).to_bytes(4, "little") + b"\0" * 344
    (source_root / "FunRaw" / "S001" / "rest.nii").write_bytes(nifti_header)
    (source_root / "T1Raw" / "S001" / "anat.nii").write_bytes(nifti_header)
    (source_root / "Results").mkdir()

    with TestClient(create_app(service=service)) as client:
        response = client.post("/api/v1/workspaces/check", json={"path": str(source_root)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "dpabi_ready"
    assert payload["input_stage"] == "funraw"
    assert payload["functional_subject_count"] == 1
    assert payload["anatomical_subject_count"] == 1
    assert payload["output_directories"] == ["Results"]
    assert payload["blocking_issues"] == []


def test_chat_conversation_persists_turns_and_rag_tool_calls(
    service: NeuroAgentService,
) -> None:
    with TestClient(create_app(service=service)) as client:
        created = client.post(
            "/api/v1/conversations",
            json={"mode": "chat"},
            headers={"Idempotency-Key": "chat-conversation-create"},
        )
        assert created.status_code == 201
        conversation_id = created.json()["conversation_id"]

        first = client.post(
            f"/api/v1/conversations/{conversation_id}/turns",
            json={"content": "ALFF 与 fALFF 有什么区别?"},
            headers={"Idempotency-Key": "chat-conversation-turn-1"},
        )
        second = client.post(
            f"/api/v1/conversations/{conversation_id}/turns",
            json={"content": "这些指标需要什么输入?"},
            headers={"Idempotency-Key": "chat-conversation-turn-2"},
        )
        restored = client.get(f"/api/v1/conversations/{conversation_id}")
        listed = client.get("/api/v1/conversations?mode=chat")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["tool_call"]["tool_name"] == "rag_rsfmri_question"
    assert first.json()["tool_call"]["status"] == "succeeded"
    payload = restored.json()
    assert [item["role"] for item in payload["messages"]] == [
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert len(payload["tool_calls"]) == 2
    assert [item["conversation_id"] for item in listed.json()] == [conversation_id]


def test_rebinding_provider_connection_updates_one_record(
    service: NeuroAgentService,
) -> None:
    def body(model: str) -> dict[str, object]:
        return {
            "profile": {
                "id": "provider-connection",
                "provider": "openai-compatible",
                "base_url": "https://provider.example/v1",
                "model": model,
                "api_key_env": "PROVIDER_API_KEY",
                "priority": 100,
                "capabilities": ["json_object"],
                "timeout_seconds": 45,
            },
            "api_key": None,
        }

    with TestClient(create_app(service=service)) as client:
        first = client.post(
            "/api/v1/model-profiles",
            json=body("first-model"),
            headers={"Idempotency-Key": "provider-bind-first"},
        )
        second = client.post(
            "/api/v1/model-profiles",
            json=body("second-model"),
            headers={"Idempotency-Key": "provider-bind-second"},
        )
        listed = client.get("/api/v1/model-profiles")

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["version"] == 2
    assert second.json()["profile"]["model"] == "second-model"
    assert len(listed.json()) == 1


def test_chat_uses_selected_llm_and_explicit_web_search(
    tmp_path: Path,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MockProvider(
        [
            ProviderResponse(
                content="联网证据与本地方法文档一致: ALFF 反映低频振幅。",
                model="search-model",
                citations=(
                    ProviderCitation(
                        url="https://example.org/rsfmri-methods",
                        title="rs-fMRI methods",
                    ),
                ),
            )
        ]
    )
    settings = Settings(
        rag_db_dir=None,
        database_url=f"sqlite:///{(tmp_path / 'chat.sqlite').as_posix()}",
        allowed_source_roots=[source_root],
        allowed_work_root=work_root,
        redaction_salt="stable-chat-test-redaction-salt",
        secrets_file=tmp_path / ".env",
    )
    monkeypatch.setenv("CHAT_TEST_API_KEY", "test-secret")
    chat_service = build_service(
        settings,
        providers={"openai-compatible": provider},
    )
    try:
        chat_service.repository.create_model_profile(
            ModelProfile(
                id="search-profile",
                provider="openai-compatible",
                base_url="https://example.org/v1",
                model="search-model",
                api_key_env="CHAT_TEST_API_KEY",
                capabilities=frozenset({ModelCapability.WEB_SEARCH}),
            )
        )
        with TestClient(create_app(service=chat_service)) as client:
            conversation = client.post(
                "/api/v1/conversations",
                json={"mode": "chat", "preferred_profile_id": "search-profile"},
                headers={"Idempotency-Key": "web-chat-create"},
            ).json()
            response = client.post(
                f"/api/v1/conversations/{conversation['conversation_id']}/turns",
                json={
                    "content": "ALFF 的生理含义是什么?",
                    "preferred_profile_id": "search-profile",
                    "model": "selected-search-model",
                    "allow_remote_search": True,
                },
                headers={"Idempotency-Key": "web-chat-turn"},
            )
    finally:
        chat_service.close()

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["tool_call"]["tool_name"] == "rsfmri_chat_llm"
    assert body["assistant_message"]["payload"]["rag"]["remote_search_used"] is True
    assert body["assistant_message"]["payload"]["model"]["profile_id"] == "search-profile"
    evidence = body["assistant_message"]["payload"]["rag"]["answer"]["evidence"]
    assert any(item["source"] == "https://example.org/rsfmri-methods" for item in evidence)
    assert provider.request_options == [{"web_search": True, "json_object": False}]
    assert provider.requests[0][0].model == "selected-search-model"


def test_work_conversation_checks_selected_workspace_and_records_tool(
    service: NeuroAgentService, source_root: Path
) -> None:
    (source_root / "FunRaw" / "S001").mkdir(parents=True)
    nifti_header = (348).to_bytes(4, "little") + b"\0" * 344
    (source_root / "FunRaw" / "S001" / "rest.nii").write_bytes(nifti_header)

    with TestClient(create_app(service=service)) as client:
        created = client.post(
            "/api/v1/conversations",
            json={"mode": "work"},
            headers={"Idempotency-Key": "work-conversation-create"},
        ).json()
        turn = client.post(
            f"/api/v1/conversations/{created['conversation_id']}/turns",
            json={
                "content": "检查这个工作区是否符合 DPABI 格式",
                "action": "check_workspace",
                "workspace_path": str(source_root),
            },
            headers={"Idempotency-Key": "work-conversation-check"},
        )

    assert turn.status_code == 201
    body = turn.json()
    assert body["conversation"]["workspace_path"] == str(source_root.resolve())
    assert body["tool_call"]["tool_name"] == "check_workspace"
    assert body["assistant_message"]["payload"]["workspace_check"]["kind"] == "dpabi_ready"


def test_work_conversation_requires_approved_plan_and_run_confirmation(
    service: NeuroAgentService,
) -> None:
    with TestClient(create_app(service=service)) as client:
        created = client.post(
            "/api/v1/conversations",
            json={"mode": "work"},
            headers={"Idempotency-Key": "work-start-conversation"},
        ).json()
        turn = client.post(
            f"/api/v1/conversations/{created['conversation_id']}/turns",
            json={"content": "启动预处理", "action": "start_preprocessing"},
            headers={"Idempotency-Key": "work-start-awaiting-confirmation"},
        )
        progress = client.post(
            f"/api/v1/conversations/{created['conversation_id']}/turns",
            json={"content": "现在运行到哪里了?", "action": "get_progress"},
            headers={"Idempotency-Key": "work-progress-without-run"},
        )

    assert turn.status_code == 201
    body = turn.json()
    assert body["tool_call"]["status"] == "awaiting_confirmation"
    assert body["conversation"]["active_run_id"] is None
    assert progress.status_code == 201
    assert progress.json()["tool_call"] is None
    assert "尚未启动运行" in progress.json()["assistant_message"]["content"]


def test_work_conversation_rejects_non_preprocessing_plan_as_a_recorded_tool_failure(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root, key="conversation-project")
    plan = make_approved_plan(service, project.project_id)
    with TestClient(create_app(service=service)) as client:
        created = client.post(
            "/api/v1/conversations",
            json={"mode": "work"},
            headers={"Idempotency-Key": "invalid-plan-conversation"},
        ).json()
        turn = client.post(
            f"/api/v1/conversations/{created['conversation_id']}/turns",
            json={
                "content": "确认启动 DPABI",
                "action": "start_preprocessing",
                "project_id": project.project_id,
                "plan_revision_id": plan.plan_revision_id,
                "expected_plan_hash": plan.plan_hash,
                "real_execution_confirmed": True,
            },
            headers={"Idempotency-Key": "invalid-plan-start"},
        )

    assert turn.status_code == 201
    assert turn.json()["tool_call"]["status"] == "failed"
    assert "SkillPlan" in turn.json()["assistant_message"]["content"]


def test_write_endpoint_requires_idempotency_header(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    with TestClient(create_app(service=service)) as client:
        response = client.post(
            "/api/v1/projects",
            json={
                "name": "missing header",
                "source_roots": [str(source_root)],
                "work_root": str(work_root / "project"),
            },
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "request_validation_failed"


def test_environment_probe_never_starts_matlab(service: NeuroAgentService) -> None:
    with TestClient(create_app(service=service)) as client:
        response = client.get("/api/v1/environment/probe")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ready"] is False
        assert all(
            "no MATLAB process started" in item["evidence"] for item in payload["components"]
        )
        assert all("configured_path" not in item for item in payload["components"])
        assert str(service.settings.matlab_executable) not in response.text


def test_environment_config_accepts_user_selected_local_paths(
    service: NeuroAgentService, tmp_path: Path
) -> None:
    matlab = tmp_path / "MATLAB" / "bin" / "matlab.exe"
    spm = tmp_path / "MATLAB" / "toolbox" / "spm"
    dpabi = tmp_path / "MATLAB" / "toolbox" / "DPABI-custom"
    matlab.parent.mkdir(parents=True)
    spm.mkdir(parents=True)
    dpabi.mkdir(parents=True)
    matlab.write_bytes(b"local matlab executable placeholder")

    with TestClient(create_app(service=service)) as client:
        response = client.put(
            "/api/v1/environment/config",
            json={
                "matlab_executable": str(matlab),
                "spm_dir": str(spm),
                "dpabi_dir": str(dpabi),
                "matlab_version": "R-local",
                "spm_version": "SPM-local",
                "dpabi_version": "DPABI-local",
            },
        )
        assert response.status_code == 200
        assert response.json()["configured"] is True
        assert response.json()["dpabi_version"] == "DPABI-local"

        restored = client.get("/api/v1/environment/config")
        assert restored.status_code == 200
        assert restored.json()["matlab_executable"] == str(matlab.resolve())


def test_unexpected_error_uses_safe_envelope(
    service: NeuroAgentService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode() -> None:
        raise RuntimeError("private implementation detail")

    monkeypatch.setattr(service, "health", explode)
    with TestClient(create_app(service=service), raise_server_exceptions=False) as client:
        response = client.get("/api/v1/health", headers={"X-Trace-ID": "test-trace-id"})

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_server_error",
            "message": "服务发生未预期错误, 请使用 trace ID 查看本地日志。",
            "details": {},
            "trace_id": "test-trace-id",
        }
    }
    assert "private implementation detail" not in response.text


def test_sse_returns_persisted_run_events_and_supports_resume_cursor(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root)
    plan = make_approved_plan(service, project.project_id)
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "sse-run",
    )
    build_worker(service, worker_id="sse-worker").run_once()
    with TestClient(create_app(service=service)) as client:
        response = client.get(f"/api/v1/runs/{run.run_id}/events?once=true")
        assert response.status_code == 200
        assert "event: RunQueued" in response.text
        assert "event: WorkflowTransitioned" in response.text
        last_id = service.list_run_events(run.run_id)[-1].event_id
        resumed = client.get(
            f"/api/v1/runs/{run.run_id}/events?once=true",
            headers={"Last-Event-ID": str(last_id)},
        )
        assert resumed.status_code == 200
        assert resumed.text == ""
