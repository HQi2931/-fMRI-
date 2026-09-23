from __future__ import annotations

import json
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
from neuroagent.application.contracts import (
    ApprovalCreate,
    ApprovalDecision,
    DatasetCreate,
    ManifestScanRequest,
    ProjectCreate,
    RunCreate,
    SkillPlanIntent,
)
from neuroagent.application.services import NeuroAgentService
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service, build_worker

from .conftest import make_approved_plan, make_project
from .test_environment import _fake_stack
from .test_science_agent_api import _minimal_preprocessing_payload


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
    assert payload["issues"] == []


def test_workspace_check_reports_format_risks_without_blocking(
    service: NeuroAgentService, source_root: Path
) -> None:
    nifti_header = (348).to_bytes(4, "little") + b"\0" * 344
    for stage in ("FunRaw", "FunImg"):
        target = source_root / stage / "S001"
        target.mkdir(parents=True)
        (target / "rest.nii").write_bytes(nifti_header)

    with TestClient(create_app(service=service)) as client:
        response = client.post("/api/v1/workspaces/check", json={"path": str(source_root)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["issues"]
    assert payload["input_stage"] is None


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
                content='{"intent":"knowledge_query","query":"ALFF 的生理含义"}',
                model="search-model",
            ),
            ProviderResponse(
                content="联网证据与本地方法文档一致: ALFF 反映低频振幅。",
                model="search-model",
                citations=(
                    ProviderCitation(
                        url="https://example.org/rsfmri-methods",
                        title="rs-fMRI methods",
                    ),
                ),
            ),
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
    assert provider.request_options == [
        {"web_search": False, "json_object": True},
        {"web_search": True, "json_object": False},
    ]
    assert provider.requests[0][0].model == "selected-search-model"


def test_work_conversation_rejects_removed_workspace_check_action(
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

    assert turn.status_code == 422


def test_work_format_advisor_is_not_called_for_removed_check_action(
    tmp_path: Path,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = MockProvider(
        [
            ProviderResponse(
                content=json.dumps(
                    {
                        "summary": "建议先明确唯一输入阶段和受试者配对。",
                        "proposed_skill_request": None,
                        "expected_layout": ["FunRaw/<subject>"],
                        "adjustment_steps": ["为每个受试者保留一份功能像。"],
                        "recommended_target_stage": "FunRaw",
                        "warnings": [],
                        "unresolved_questions": ["是否选择 FunRaw?"],
                        "requires_user_confirmation": True,
                    }
                ),
                model="format-model",
            )
        ]
    )
    settings = Settings(
        rag_db_dir=None,
        database_url=f"sqlite:///{(tmp_path / 'format-advisor.sqlite').as_posix()}",
        allowed_source_roots=[source_root],
        allowed_work_root=work_root,
        redaction_salt="stable-format-advisor-salt",
        secrets_file=tmp_path / ".env",
    )
    monkeypatch.setenv("FORMAT_ADVISOR_API_KEY", "test-secret")
    app_service = build_service(settings, providers={"openai-compatible": provider})
    try:
        app_service.repository.create_model_profile(
            ModelProfile(
                id="format-advisor",
                provider="openai-compatible",
                base_url="https://provider.example/v1",
                model="format-model",
                api_key_env="FORMAT_ADVISOR_API_KEY",
                capabilities=frozenset({ModelCapability.JSON_OBJECT}),
            )
        )
        (source_root / "FunRaw" / "S001").mkdir(parents=True)
        (source_root / "FunImg" / "S001").mkdir(parents=True)
        nifti_header = (348).to_bytes(4, "little") + b"\0" * 344
        (source_root / "FunRaw" / "S001" / "rest.nii").write_bytes(nifti_header)
        (source_root / "FunImg" / "S001" / "rest.nii").write_bytes(nifti_header)
        with TestClient(create_app(service=app_service)) as client:
            created = client.post(
                "/api/v1/conversations",
                json={"mode": "work"},
                headers={"Idempotency-Key": "format-advisor-conversation"},
            ).json()
            response = client.post(
                f"/api/v1/conversations/{created['conversation_id']}/turns",
                json={
                    "content": "检查 D:\\secret\\workspace 的格式",
                    "action": "check_workspace",
                    "workspace_path": str(source_root),
                    "preferred_profile_id": "format-advisor",
                },
                headers={"Idempotency-Key": "format-advisor-turn"},
            )
    finally:
        app_service.close()

    assert response.status_code == 422
    assert not provider.requests


def test_work_conversation_registers_selected_workspace_and_freezes_manifest(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    (source_root / "FunRaw" / "S001").mkdir(parents=True)
    nifti_header = (348).to_bytes(4, "little") + b"\0" * 344
    (source_root / "FunRaw" / "S001" / "rest.nii").write_bytes(nifti_header)

    with TestClient(create_app(service=service)) as client:
        created = client.post(
            "/api/v1/conversations",
            json={"mode": "work"},
            headers={"Idempotency-Key": "work-setup-create"},
        ).json()
        response = client.post(
            f"/api/v1/conversations/{created['conversation_id']}/turns",
            json={
                "content": "注册工作区并冻结清单",
                "action": "setup_workspace",
                "workspace_path": str(source_root),
                "workspace_setup": {
                    "project_name": "Work project",
                    "dataset_name": "Resting state",
                    "work_root": str(work_root / "work-project"),
                },
            },
            headers={"Idempotency-Key": "work-setup-turn"},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["tool_call"]["tool_name"] == "setup_workspace"
    assert body["tool_call"]["error"] is None, body["tool_call"]["error"]
    assert body["tool_call"]["status"] == "succeeded"
    assert (
        body["conversation"]["project_id"]
        == body["assistant_message"]["payload"]["project"]["project_id"]
    )
    assert (
        body["assistant_message"]["payload"]["dataset"]["current_manifest_id"]
        == body["assistant_message"]["payload"]["manifest"]["manifest_id"]
    )
    assert body["assistant_message"]["payload"]["manifest"]["profile"]["subject_count"] == 1
    stored_dataset = service.get_dataset(
        body["assistant_message"]["payload"]["dataset"]["dataset_id"]
    )
    assert (
        stored_dataset.current_manifest_id
        == body["assistant_message"]["payload"]["manifest"]["manifest_id"]
    )


def test_work_conversation_rejects_removed_organization_preview_action(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    (source_root / "FunRaw" / "S001").mkdir(parents=True)
    (source_root / "T1Raw" / "S001").mkdir(parents=True)
    nifti_header = (348).to_bytes(4, "little") + b"\0" * 344
    functional = source_root / "FunRaw" / "S001" / "rest.nii"
    anatomical = source_root / "T1Raw" / "S001" / "anat.nii"
    processed = source_root / "FunImgARW" / "S001" / "processed.nii"
    functional.write_bytes(nifti_header)
    anatomical.write_bytes(nifti_header)
    processed.parent.mkdir(parents=True)
    processed.write_bytes(nifti_header)
    original_functional = functional.read_bytes()
    with TestClient(create_app(service=service)) as client:
        created = client.post(
            "/api/v1/conversations",
            json={"mode": "work"},
            headers={"Idempotency-Key": "organization-preview-conversation"},
        ).json()
        setup = client.post(
            f"/api/v1/conversations/{created['conversation_id']}/turns",
            json={
                "content": "注册工作区并冻结清单",
                "action": "setup_workspace",
                "workspace_path": str(source_root),
                "workspace_setup": {
                    "project_name": "Organization preview",
                    "dataset_name": "Resting state",
                    "work_root": str(work_root / "organization-project"),
                },
            },
            headers={"Idempotency-Key": "organization-preview-setup"},
        )
        setup_payload = setup.json()["assistant_message"]["payload"]
        preview = client.post(
            f"/api/v1/conversations/{created['conversation_id']}/turns",
            json={
                "content": "生成 FunRaw 整理预览",
                "action": "preview_workspace_organization",
                "workspace_path": str(source_root),
                "project_id": setup_payload["project"]["project_id"],
                "dataset_id": setup_payload["dataset"]["dataset_id"],
                "organization_target_stage": "FunRaw",
            },
            headers={"Idempotency-Key": "organization-preview-turn"},
        )
        processed_preview = client.post(
            f"/api/v1/conversations/{created['conversation_id']}/turns",
            json={
                "content": "生成 FunImgARW 整理预览",
                "action": "preview_workspace_organization",
                "workspace_path": str(source_root),
                "project_id": setup_payload["project"]["project_id"],
                "dataset_id": setup_payload["dataset"]["dataset_id"],
                "organization_target_stage": "FunImgARW",
            },
            headers={"Idempotency-Key": "organization-preview-processed-turn"},
        )

    assert setup.status_code == 201, setup.text
    assert preview.status_code == 422, preview.text
    assert processed_preview.status_code == 422, processed_preview.text
    assert functional.read_bytes() == original_functional
    assert not (source_root / "FunImg").exists()
    assert processed.read_bytes() == nifti_header


def test_work_conversation_plan_request_waits_for_explicit_scientific_parameters(
    service: NeuroAgentService,
) -> None:
    with TestClient(create_app(service=service)) as client:
        created = client.post(
            "/api/v1/conversations",
            json={"mode": "work"},
            headers={"Idempotency-Key": "work-plan-create"},
        ).json()
        response = client.post(
            f"/api/v1/conversations/{created['conversation_id']}/turns",
            json={"content": "帮我准备一个 ALFF 预处理方案"},
            headers={"Idempotency-Key": "work-plan-turn"},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["tool_call"]["status"] == "awaiting_confirmation"
    assert "科研参数" in body["assistant_message"]["content"]
    assert "没有单独的方案表单" in body["assistant_message"]["content"]
    assert "work_cards" not in body["assistant_message"]["payload"]


def test_work_conversation_handles_greetings_and_form_questions_naturally(
    service: NeuroAgentService,
) -> None:
    with TestClient(create_app(service=service)) as client:
        created = client.post(
            "/api/v1/conversations",
            json={"mode": "work"},
            headers={"Idempotency-Key": "work-conversation-natural-dialogue"},
        ).json()
        greeting = client.post(
            f"/api/v1/conversations/{created['conversation_id']}/turns",
            json={"content": "你好"},
            headers={"Idempotency-Key": "work-natural-greeting"},
        )
        form_help = client.post(
            f"/api/v1/conversations/{created['conversation_id']}/turns",
            json={"content": "方案表单在哪里"},
            headers={"Idempotency-Key": "work-natural-form-help"},
        )

    assert greeting.status_code == 201
    assert "你好" in greeting.json()["assistant_message"]["content"]
    assert "我已记录" not in greeting.json()["assistant_message"]["content"]
    assert form_help.status_code == 201
    assert "没有单独的方案表单" in form_help.json()["assistant_message"]["content"]
    assert "工作要求" not in form_help.json()["assistant_message"]["content"]


def test_work_conversation_routes_qc_and_statistical_result_queries(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root, key="work-query-project")
    with TestClient(create_app(service=service)) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={"mode": "work", "project_id": project.project_id},
            headers={"Idempotency-Key": "work-query-conversation"},
        ).json()
        qc = client.post(
            f"/api/v1/conversations/{conversation['conversation_id']}/turns",
            json={"content": "查看 QC 状态"},
            headers={"Idempotency-Key": "work-query-qc"},
        )
        results = client.post(
            f"/api/v1/conversations/{conversation['conversation_id']}/turns",
            json={"content": "查看统计结果"},
            headers={"Idempotency-Key": "work-query-results"},
        )

    assert qc.status_code == 201
    assert qc.json()["tool_call"]["status"] == "awaiting_confirmation"
    assert "QC 审核" in qc.json()["assistant_message"]["content"]
    assert "QC review" not in qc.json()["assistant_message"]["content"]
    assert results.status_code == 201
    assert results.json()["tool_call"]["status"] == "succeeded"
    assert results.json()["assistant_message"]["payload"]["statistical_results"] == []
    assert "work_cards" not in results.json()["assistant_message"]["payload"]


def test_removed_work_card_endpoints_are_not_exposed(
    service: NeuroAgentService,
) -> None:
    with TestClient(create_app(service=service)) as client:
        capabilities = client.get("/api/v1/work/capabilities")
        conversation = client.post(
            "/api/v1/conversations",
            json={"mode": "work"},
            headers={"Idempotency-Key": "work-catalog-conversation"},
        ).json()
        unclear = client.post(
            f"/api/v1/conversations/{conversation['conversation_id']}/turns",
            json={"content": "帮我处理一下"},
            headers={"Idempotency-Key": "work-catalog-clarify"},
        )
        card_action = client.post(
            f"/api/v1/conversations/{conversation['conversation_id']}/cards/missing/actions",
            json={"expected_version": 1, "operation": "saveDraft", "args": [{}]},
            headers={"Idempotency-Key": "removed-card-action"},
        )

    assert capabilities.status_code == 404
    assert card_action.status_code == 404
    assert unclear.status_code == 201
    assert "我可以协助" in unclear.json()["assistant_message"]["content"]
    assert "work_cards" not in unclear.json()["assistant_message"]["payload"]


def test_work_conversation_naturally_prepares_demo_alff_plan_without_cards(
    service: NeuroAgentService,
    source_root: Path,
) -> None:
    subject = source_root / "FunImg" / "Sub_001"
    subject.mkdir(parents=True)
    (subject / "rest.nii").write_bytes((348).to_bytes(4, "little") + b"\0" * 344)
    (subject / "rest.json").write_text('{"RepetitionTime":2.0}', encoding="utf-8")

    with TestClient(create_app(service=service)) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={"mode": "work", "workspace_path": str(source_root)},
            headers={"Idempotency-Key": "demo-alff-conversation"},
        ).json()
        response = client.post(
            f"/api/v1/conversations/{conversation['conversation_id']}/turns",
            json={
                "content": "使用当前 DemoData 的 Sub_001 完成 ALFF 预处理, 先准备并展示方案",
                "workspace_path": str(source_root),
            },
            headers={"Idempotency-Key": "demo-alff-prepare"},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    payload = body["assistant_message"]["payload"]
    plan = payload["plan_revision"]
    skill_plan = payload["skill_plan"]
    parameters = dict(skill_plan["resolved_parameters"])
    assert body["tool_call"]["tool_name"] == "prepare_demo_alff"
    assert plan["state"] == "awaiting_approval"
    assert parameters["alff_falff"]["requested_metrics"] == ["alff"]
    assert parameters["alff_falff"]["mask_source"] == "dpabi_auto"
    assert parameters["alff_falff"]["mask_artifact_id"] is None
    assert parameters["preprocessing"]["tr_seconds"] == 2.0
    assert parameters["preprocessing"]["expected_time_points"] == 234
    assert "计划哈希" not in body["assistant_message"]["content"]
    assert "ID" not in body["assistant_message"]["content"]
    assert "work_cards" not in payload


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
                "qc_mode": "require",
            },
            headers={"Idempotency-Key": "invalid-plan-start"},
        )

    assert turn.status_code == 201
    assert turn.json()["tool_call"]["status"] == "awaiting_confirmation"
    assert "确认" in turn.json()["assistant_message"]["content"]


def test_work_conversation_starts_approved_preprocessing_and_restores_progress(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    (source / "FunRaw" / "sub-01").mkdir(parents=True)
    work.mkdir()
    nifti_header = (348).to_bytes(4, "little") + b"\0" * 344
    (source / "FunRaw" / "sub-01" / "rest.nii").write_bytes(nifti_header)
    settings = _fake_stack(tmp_path / "fake", "preprocessing").model_copy(
        update={
            "database_url": f"sqlite:///{(tmp_path / 'work-conversation.sqlite').as_posix()}",
            "allowed_source_roots": [source],
            "allowed_work_root": work,
            "enable_real_execution": True,
        }
    )
    service = build_service(settings)
    try:
        project = service.create_project(
            ProjectCreate(
                name="Conversation run",
                source_roots=[str(source)],
                work_root=str(work / "project"),
            ),
            "conversation-run-project",
        )
        dataset = service.create_dataset(
            project.project_id,
            DatasetCreate(
                name="DPABI input",
                source_path=str(source),
                expected_project_version=project.version,
            ),
            "conversation-run-dataset",
        )
        manifest = service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=dataset.version),
            "conversation-run-manifest",
        )
        with TestClient(create_app(service=service)) as client:
            conversation = client.post(
                "/api/v1/conversations",
                json={
                    "mode": "work",
                    "project_id": project.project_id,
                    "workspace_path": str(source),
                },
                headers={"Idempotency-Key": "conversation-run-create"},
            ).json()
            intent = SkillPlanIntent(
                project_id=project.project_id,
                dataset_ref=dataset.dataset_id,
                input_manifest_hash=manifest.content_hash,
                requested_metrics=(),
                primary_outputs=(),
                study_protocol_ref="explicit-test-protocol",
                request_preprocessing=True,
                preprocessing=_minimal_preprocessing_payload(),
            )
            prepared = client.post(
                f"/api/v1/conversations/{conversation['conversation_id']}/turns",
                json={
                    "content": "使用这些显式参数生成预处理方案",
                    "action": "prepare_preprocessing_plan",
                    "skill_plan_intent": intent.model_dump(mode="json"),
                    "expected_project_version": service.get_project(project.project_id).version,
                },
                headers={"Idempotency-Key": "conversation-run-plan"},
            )
            assert prepared.status_code == 201, prepared.text
            prepared_body = prepared.json()
            assert prepared_body["tool_call"]["status"] == "succeeded"
            assert (
                prepared_body["assistant_message"]["payload"]["skill_plan"]["dataset_ref"]
                == dataset.dataset_id
            )
            plan = service.get_plan(
                prepared_body["assistant_message"]["payload"]["plan_revision"]["plan_revision_id"]
            )
            service.approve_plan(
                plan.plan_revision_id,
                ApprovalCreate(
                    expected_version=plan.version,
                    plan_hash=plan.plan_hash,
                    actor="test researcher",
                    decision=ApprovalDecision.APPROVED,
                    reason="reviewed explicit synthetic protocol",
                ),
                "conversation-run-approval",
            )
            preview = client.post(
                f"/api/v1/conversations/{conversation['conversation_id']}/turns",
                json={
                    "content": "复核运行风险",
                    "action": "preview_preprocessing_run",
                    "plan_revision_id": plan.plan_revision_id,
                    "expected_plan_hash": plan.plan_hash,
                },
                headers={"Idempotency-Key": "conversation-run-preview"},
            )
            assert preview.status_code == 201, preview.text
            preview_body = preview.json()
            assert preview_body["tool_call"]["status"] == "succeeded"
            assert preview_body["assistant_message"]["payload"]["run_preflight"]["ready"] is True
            assert (
                preview_body["assistant_message"]["payload"]["run_preflight"]["workspace"][
                    "free_space_bytes"
                ]
                > 0
            )
            started = client.post(
                f"/api/v1/conversations/{conversation['conversation_id']}/turns",
                json={
                    "content": "确认启动已审批预处理",
                    "action": "start_preprocessing",
                    "plan_revision_id": plan.plan_revision_id,
                    "expected_plan_hash": plan.plan_hash,
                    "real_execution_confirmed": True,
                    "qc_mode": "require",
                },
                headers={"Idempotency-Key": "conversation-run-start"},
            )
            progress = client.post(
                f"/api/v1/conversations/{conversation['conversation_id']}/turns",
                json={"content": "查看运行进度", "action": "get_progress"},
                headers={"Idempotency-Key": "conversation-run-progress"},
            )

        assert started.status_code == 201, started.text
        started_body = started.json()
        assert started_body["tool_call"]["status"] == "awaiting_confirmation"
        started_content = started_body["assistant_message"]["content"]
        assert "确认" in started_content
        assert started_body["conversation"]["active_run_id"] is None
        assert progress.status_code == 201, progress.text
        progress_body = progress.json()
        assert progress_body["conversation"]["active_run_id"] is None
    finally:
        service.close()


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
