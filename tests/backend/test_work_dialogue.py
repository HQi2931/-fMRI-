"""Model understanding, draft continuity and execution authorization regressions."""

# ruff: noqa: RUF001
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from neuroagent.agent.gateway import ChatGatewayResult
from neuroagent.agent.models import ModelProfile, ProviderResponse
from neuroagent.api import create_app
from neuroagent.application.contracts import ConversationCreate, ConversationTurnCreate
from neuroagent.application.conversation_work import WorkActionResult
from neuroagent.application.errors import ApplicationError, ConflictError
from neuroagent.application.services import NeuroAgentService
from neuroagent.application.work_draft import draft_hash
from tests.backend.test_work_draft import complete_draft


def generated(action: str, patch: dict | None = None, answer: str = "请继续补充参数"):
    return ChatGatewayResult(
        response=ProviderResponse(
            model="chosen-model",
            content=json.dumps({"answer": answer, "action": action, "draft_patch": patch or {}}),
        ),
        selected_profile_id="dialogue-model",
        context_hash="a" * 64,
        attempted_profile_ids=("dialogue-model",),
        remote_search_used=False,
        redaction_count=0,
    )


def configure(service: NeuroAgentService, monkeypatch: pytest.MonkeyPatch, responses: list):
    service.repository.create_model_profile(
        ModelProfile(
            id="dialogue-model",
            provider="openai-compatible",
            base_url="https://example.org/v1",
            model="chosen-model",
            api_key_env="UNUSED_MOCK_API_KEY",
            context_window_tokens=32768,
        )
    )
    model = AsyncMock(side_effect=responses)
    monkeypatch.setattr(service, "_generate_rsfmri_chat", model)
    return model


def create(client: TestClient, workspace: Path | None = None) -> str:
    result = client.post(
        "/api/v1/conversations",
        json={
            "mode": "work",
            "workspace_path": str(workspace) if workspace else None,
        },
        headers={"Idempotency-Key": "create-dialogue"},
    )
    assert result.status_code == 201, result.text
    return result.json()["conversation_id"]


def turn(client: TestClient, session: str, content: str, key: str, **fields):
    response = client.post(
        f"/api/v1/conversations/{session}/turns",
        json={"content": content, "model": "chosen-model", **fields},
        headers={"Idempotency-Key": "dialogue-" + key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_work_uses_model_and_history_for_questions_without_actions(service, monkeypatch):
    model = configure(
        service,
        monkeypatch,
        [
            generated("conversation", answer="你好！可以直接在对话里准备方案。"),
            generated("conversation", answer="QC 是质量控制，讨论它不会启动任务。"),
        ],
    )
    execute = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(service.conversation_work, "execute", execute)
    with TestClient(create_app(service=service)) as client:
        session = create(client)
        first = turn(client, session, "你好", "hello")
        second = turn(client, session, "不要运行，什么是QC", "question")
    assert "你好" in first["assistant_message"]["content"]
    assert "质量控制" in second["assistant_message"]["content"]
    assert second["tool_call"] is None
    assert model.call_args.kwargs["model"] == "chosen-model"
    assert any(item["content"] == "你好" for item in model.call_args.kwargs["recent_messages"])
    execute.assert_not_called()


def test_stage_confirmation_registers_a_fresh_workspace_without_model_execution(
    service, source_root, monkeypatch
):
    subject = source_root / "FunImg" / "sub-01"
    subject.mkdir(parents=True)
    (subject / "rest.nii").write_bytes((348).to_bytes(4, "little") + b"\0" * 344)
    response = generated("conversation", answer="建议使用 FunImg 起点。")
    response = response.__class__(
        **{
            **response.__dict__,
            "response": response.response.model_copy(
                update={
                    "content": json.dumps(
                        {
                            "answer": "建议使用 FunImg 起点。",
                            "action": "conversation",
                            "recommended_input_stage": "FunImg",
                        }
                    )
                }
            ),
        }
    )
    model = configure(service, monkeypatch, [response])
    with TestClient(create_app(service=service)) as client:
        session = create(client, source_root)
        proposed = turn(client, session, "建议起点", "stage-propose")
        payload = proposed["assistant_message"]["payload"]
        command = payload["confirmation_request"]["options"][0]["command"]
        assert "dataset" not in payload
        confirmed = turn(client, session, command, "stage-confirm")
        result = confirmed["assistant_message"]["payload"]
        assert result["manifest"]["selected_input_stage"] == "FunImg"
        assert result["dataset"]["dataset_id"]
        assert service.repository.list_runs() == []
        assert model.await_count == 1


def test_work_context_exposes_only_anonymized_workspace_profile(service) -> None:
    context = service.conversation_context.work_context(
        project_id=None,
        active_run_id=None,
        plan_revision_id=None,
        workspace_stage_candidates={
            "candidates": [],
            "selection_required": False,
            "data_profile": {
                "kind": "dpabi_ready",
                "subject_count": 2,
                "functional_subject_count": 2,
                "anatomical_subject_count": 1,
                "issues": [],
                "warnings": ["部分受试者未识别到 T1 结构像。"],
            },
        },
    )

    assert context["workspace_data_profile"]["subject_count"] == 2
    assert "subject_id" not in json.dumps(context["workspace_data_profile"])


def test_partial_parameters_persist_merge_and_old_confirmation_is_rejected(service, monkeypatch):
    model = configure(
        service,
        monkeypatch,
        [
            generated("prepare_preprocessing_plan", {"preprocessing": {"tr_seconds": 2.0}}),
            generated("prepare_preprocessing_plan", {"preprocessing": {"dummy_scans": 10}}),
            generated("prepare_preprocessing_plan", {"preprocessing": {"tr_seconds": 3.0}}),
        ],
    )
    with TestClient(create_app(service=service)) as client:
        session = create(client)
        first = turn(client, session, "生成方案，TR是2秒", "draft-1")
        old_hash = first["assistant_message"]["payload"]["work_draft"]["hash"]
        assert first["assistant_message"]["payload"]["plan_preview"]["kind"] == "dpabi_plan_preview"
        decisions = first["assistant_message"]["payload"]["planning_decisions"]
        assert "preprocessing.tr_seconds：模型建议" in first["assistant_message"]["content"]
        assert (
            next(
                item for item in decisions["choices"] if item["path"] == "preprocessing.tr_seconds"
            )["status"]
            == "proposed"
        )
        assert "confirmation_request" not in first["assistant_message"]["payload"]
        turn(client, session, "去掉10个时间点", "draft-2")
        third = turn(client, session, "更正，TR是3秒", "draft-3")
        stale = turn(client, session, f"确认方案参数 {old_hash[:12]}", "stale")
        draft = third["assistant_message"]["payload"]["work_draft"]["draft"]
    assert draft["preprocessing"] == {"tr_seconds": 3.0, "dummy_scans": 10}
    assert "planning_decisions" in third["assistant_message"]["payload"]
    assert "已变化" in stale["assistant_message"]["content"]
    assert model.await_count == 3
    assert service.list_projects() == []


@pytest.mark.parametrize("bad", ["invalid json", '{"answer":"ok","action":"run_shell"}'])
def test_malformed_model_never_dispatches(service, monkeypatch, bad):
    value = generated("conversation")
    object.__setattr__(value, "response", ProviderResponse(content=bad, model="test"))
    configure(service, monkeypatch, [value])
    execute = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(service.conversation_work, "execute", execute)
    with TestClient(create_app(service=service)) as client:
        result = turn(client, create(client), "帮我准备方案", "bad")
    assert "未执行任务" in result["assistant_message"]["content"]
    execute.assert_not_called()


def test_model_start_intent_and_ui_boolean_cannot_authorize_execution(service, monkeypatch):
    configure(service, monkeypatch, [generated("propose_run_confirmation")])
    execute = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(service.conversation_work, "execute", execute)
    with TestClient(create_app(service=service)) as client:
        result = turn(client, create(client), "不要运行", "no-run", real_execution_confirmed=True)
    assert "先补全" in result["assistant_message"]["content"]
    execute.assert_not_called()


def test_model_cannot_even_name_a_server_only_action(service, monkeypatch):
    """A privileged action is outside the model's vocabulary, not merely refused.

    ``start_preprocessing`` is a real server action (REST/UI only).  A model that
    emits it must fail schema validation and fall back to the safe message, so a
    prompt drift can never turn into a queued run.
    """

    configure(service, monkeypatch, [generated("start_preprocessing")])
    execute = Mock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(service.conversation_work, "execute", execute)
    with TestClient(create_app(service=service)) as client:
        result = turn(
            client, create(client), "帮我启动预处理", "server-only", real_execution_confirmed=True
        )

    assert "未执行任务" in result["assistant_message"]["content"]
    execute.assert_not_called()


def test_run_confirmation_options_are_server_bound(service, source_root, work_root, monkeypatch):
    from tests.backend.conftest import make_approved_plan, make_project

    configure(service, monkeypatch, [generated("propose_run_confirmation")])
    project = make_project(service, source_root, work_root, key="options-project")
    plan = make_approved_plan(service, project.project_id, manifest_hash="d" * 64)
    session = service.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id), "options-conversation"
    )
    service.repository.append_conversation_exchange(
        session.conversation_id,
        user_content="prepare",
        assistant_content="review plan",
        assistant_payload={
            "plan_revision": {
                "plan_revision_id": plan.plan_revision_id,
                "plan_hash": plan.plan_hash,
            }
        },
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=project.project_id,
        active_run_id=None,
    )
    with TestClient(create_app(service=service)) as client:
        result = turn(client, session.conversation_id, "请提供启动选项", "options-turn")
    request = result["assistant_message"]["payload"]["confirmation_request"]
    commands = [item["command"] for item in request["options"]]
    assert len(commands) == 2
    assert commands[0].startswith(f"批准并启动 {plan.plan_hash[:12]} 做头动QC ")
    assert commands[1].startswith(f"批准并启动 {plan.plan_hash[:12]} 跳过QC ")
    assert commands[0].split()[-1] != commands[1].split()[-1]
    assert service.repository.list_runs() == []


def test_confirmation_challenge_is_bound_and_one_time(service, source_root, work_root):
    from neuroagent.application.contracts import QcExecutionMode, RunCreate
    from tests.backend.conftest import make_approved_plan, make_project

    project = make_project(service, source_root, work_root, key="challenge-project")
    plan = make_approved_plan(service, project.project_id, manifest_hash="d" * 64)
    conversation = service.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id),
        "challenge-conversation",
    )
    challenge = service.repository.create_confirmation_challenge(
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        plan_revision_id=plan.plan_revision_id,
        plan_hash=plan.plan_hash,
        qc_mode=QcExecutionMode.SKIP,
    )
    run = service.create_run(
        RunCreate(
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "challenge-run",
    )
    consumed = service.repository.consume_confirmation_challenge(
        confirmation_id=challenge.confirmation_id,
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        plan_revision_id=plan.plan_revision_id,
        plan_hash=plan.plan_hash,
        qc_mode=QcExecutionMode.SKIP,
        run_id=run.run_id,
    )
    assert consumed.consumed_run_id == run.run_id
    with pytest.raises(ConflictError, match="启动确认"):
        service.repository.consume_confirmation_challenge(
            confirmation_id=challenge.confirmation_id,
            conversation_id=conversation.conversation_id,
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            plan_hash=plan.plan_hash,
            qc_mode=QcExecutionMode.SKIP,
            run_id=run.run_id,
        )


@pytest.mark.asyncio
async def test_exact_server_command_defers_and_binds_execution(service, monkeypatch):
    from neuroagent.application.work_dialogue import WorkDialogue
    from neuroagent.context.interfaces import ContextPacket

    session = service.create_conversation(ConversationCreate(mode="work"), "exact-create")
    service.repository.append_conversation_exchange(
        session.conversation_id,
        user_content="prepare",
        assistant_content="review plan",
        assistant_payload={
            "plan_revision": {
                "plan_revision_id": "server-plan",
                "plan_hash": "b" * 64,
            }
        },
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=None,
        active_run_id=None,
    )
    session = service.get_conversation(session.conversation_id)
    execute = Mock(return_value=WorkActionResult("queued"))
    monkeypatch.setattr(service.conversation_work, "execute", execute)
    model = AsyncMock(side_effect=AssertionError("confirmation does not use model"))
    result = await WorkDialogue(service.conversation_work, model).respond(
        session,
        ConversationTurnCreate(content="批准并启动 bbbbbbbbbbbb 跳过QC"),
        ContextPacket(question="confirmation"),
        workspace_path=None,
        project_id=None,
        preferred_profile_id=None,
        idempotency_key="exact-confirm",
        has_profiles=True,
    )
    execute.assert_not_called()
    assert result.commit is None
    assert "确认" in result.assistant_content


def test_work_uses_redacted_gateway_json_protocol(service, monkeypatch):
    from neuroagent.agent.providers import MockProvider
    from neuroagent.application.service_mixins.models import ModelAgentMixin

    configure(service, monkeypatch, [])
    provider = MockProvider([generated("conversation", answer="可以讨论ALFF的方法。").response])
    monkeypatch.setattr(service, "providers", {"openai-compatible": provider})
    monkeypatch.setenv("UNUSED_MOCK_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(
        service,
        "settings",
        service.settings.model_copy(
            update={
                "redaction_salt": "synthetic-work-test-salt-long",
            }
        ),
    )
    monkeypatch.setattr(
        service,
        "_generate_rsfmri_chat",
        lambda **kwargs: ModelAgentMixin._generate_rsfmri_chat(service, **kwargs),
    )
    with TestClient(create_app(service=service)) as client:
        response = turn(client, create(client), "帮我了解ALFF", "gateway")
    assert "讨论ALFF" in response["assistant_message"]["content"]
    assert provider.request_options == [{"web_search": False, "json_object": True}]
    outgoing = json.loads(provider.requests[0][1][1]["content"])
    assert "draft_schema" in outgoing["work_context"]
    assert outgoing["question"] == "帮我了解ALFF"


def test_provider_failure_preserves_previous_draft(service, monkeypatch):
    configure(
        service,
        monkeypatch,
        [
            generated("prepare_preprocessing_plan", {"preprocessing": {"tr_seconds": 2}}),
            ApplicationError("unavailable", "test", status_code=503),
            generated("prepare_preprocessing_plan", {"preprocessing": {"dummy_scans": 10}}),
        ],
    )
    with TestClient(create_app(service=service)) as client:
        session = create(client)
        turn(client, session, "TR是2秒", "first")
        failed = turn(client, session, "再补充参数", "failure")
        final = turn(client, session, "去掉10个时间点", "retry")
    assert "草稿仍保留" in failed["assistant_message"]["content"]
    draft = final["assistant_message"]["payload"]["work_draft"]["draft"]
    assert draft["preprocessing"] == {"tr_seconds": 2, "dummy_scans": 10}


def test_truncated_work_response_records_safe_finish_reason(service, monkeypatch, caplog):
    value = generated("conversation")
    object.__setattr__(
        value,
        "response",
        ProviderResponse(
            model="chosen-model",
            content='{"answer":"incomplete',
            finish_reason="length",
            usage={"completion_tokens": 2048, "total_tokens": 5000},
        ),
    )
    configure(service, monkeypatch, [value])
    with TestClient(create_app(service=service)) as client:
        failed = turn(client, create(client), "检查结构", "truncated")
    diagnostic = failed["assistant_message"]["payload"]["model_error"]
    assert diagnostic["code"] == "work_response_truncated"
    assert diagnostic["finish_reason"] == "length"
    assert diagnostic["usage"]["completion_tokens"] == 2048
    assert "token 上限" in failed["assistant_message"]["content"]
    assert "incomplete" not in caplog.text


def test_work_schema_error_does_not_log_model_values_or_unknown_keys(service, monkeypatch, caplog):
    value = generated("conversation")
    object.__setattr__(
        value,
        "response",
        ProviderResponse(
            model="chosen-model",
            content=json.dumps(
                {
                    "answer": "正常回答",
                    "action": "conversation",
                    "private-model-field": "synthetic-secret-content",
                }
            ),
            finish_reason="stop",
        ),
    )
    configure(service, monkeypatch, [value])
    with TestClient(create_app(service=service)) as client:
        failed = turn(client, create(client), "检查结构", "schema-error")
    diagnostic = failed["assistant_message"]["payload"]["model_error"]
    assert diagnostic["code"] == "work_response_invalid"
    assert diagnostic["issues"] == [{"field": "root_or_unknown_field", "type": "extra_forbidden"}]
    assert "synthetic-secret-content" not in caplog.text
    assert "private-model-field" not in caplog.text
    assert "synthetic-secret-content" not in json.dumps(diagnostic)


def test_work_provider_error_records_status_without_private_exception_text(
    service, monkeypatch, caplog
):
    from neuroagent.agent.providers import ProviderError

    error = ApplicationError("chat_model_unavailable", "synthetic-private-error", status_code=503)
    error.__cause__ = ProviderError("provider rejected request (401)")
    configure(service, monkeypatch, [error])
    with TestClient(create_app(service=service)) as client:
        failed = turn(client, create(client), "检查结构", "provider-error")
    diagnostic = failed["assistant_message"]["payload"]["model_error"]
    assert diagnostic["code"] == "chat_model_unavailable"
    assert diagnostic["provider_status"] == 401
    assert diagnostic["stage"] == "model_request"
    assert "synthetic-private-error" not in caplog.text


def test_complete_review_binds_server_manifest_only_after_confirmation(
    service,
    monkeypatch,
    source_root,
):
    draft = complete_draft()
    configure(
        service,
        monkeypatch,
        [
            generated(
                "prepare_preprocessing_plan",
                draft.model_dump(
                    mode="json",
                    exclude={"confirmed_hash"},
                ),
            )
        ],
    )
    compile_plan = Mock(
        return_value=WorkActionResult(
            "方案已生成",
            payload={
                "plan_revision": {"plan_revision_id": "test-plan", "plan_hash": "b" * 64},
            },
        )
    )
    monkeypatch.setattr(service.conversation_work, "_prepare_preprocessing_plan", compile_plan)
    with TestClient(create_app(service=service)) as client:
        session = create(client, source_root)
        preview = turn(client, session, "按我提供的完整参数准备方案", "review")
        assert service.list_projects() == []
        compile_plan.assert_not_called()
        confirmed = turn(client, session, f"确认方案参数 {draft_hash(draft)[:12]}", "confirm")
        repeated = turn(client, session, f"确认方案参数 {draft_hash(draft)[:12]}", "confirm")
    assert confirmed == repeated
    assert "方案已生成" in confirmed["assistant_message"]["content"], confirmed
    compile_plan.assert_called_once()
    intent = compile_plan.call_args.args[0].skill_plan_intent
    assert intent.project_id == service.list_projects()[0].project_id
    assert len(intent.input_manifest_hash) == 64
    assert all(p.source == "user" for p in intent.preprocessing.provenance)
    assert "确认方案参数" in preview["assistant_message"]["content"]
    confirmation = preview["assistant_message"]["payload"]["confirmation_request"]
    assert confirmation["title"] == "确认方案参数"
    assert confirmation["options"][0]["command"] == f"确认方案参数 {draft_hash(draft)[:12]}"
    assert confirmed["assistant_message"]["payload"]["work_draft"]["compiled"]


@pytest.mark.asyncio
async def test_concurrent_change_rejects_deferred_side_effects(service, monkeypatch):
    from neuroagent.application.work_dialogue import WorkDialogue

    session = service.create_conversation(ConversationCreate(mode="work"), "concurrent-create")
    entered, release = asyncio.Event(), asyncio.Event()
    mutation = Mock(return_value=WorkActionResult("must not commit"))

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return WorkActionResult("", commit=mutation)

    monkeypatch.setattr(WorkDialogue, "respond", delayed)
    task = asyncio.create_task(
        service.send_conversation_turn(
            session.conversation_id,
            ConversationTurnCreate(content="confirm"),
            "concurrent-turn",
        )
    )
    await entered.wait()
    service.repository.append_conversation_exchange(
        session.conversation_id,
        user_content="参数已变更",
        assistant_content="请重新确认",
        assistant_payload={},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=None,
        active_run_id=None,
    )
    release.set()
    with pytest.raises(ConflictError):
        await task
    mutation.assert_not_called()


@pytest.mark.asyncio
async def test_required_schema_is_counted_and_small_context_blocks_provider(service, monkeypatch):
    from neuroagent.application.work_dialogue import WorkDialogue
    from neuroagent.context.interfaces import ContextPacket

    session = service.create_conversation(ConversationCreate(mode="work"), "budget-create")
    model = AsyncMock(side_effect=AssertionError("must not exceed context budget"))
    result = await WorkDialogue(service.conversation_work, model).respond(
        session,
        ConversationTurnCreate(content="你好"),
        ContextPacket(question="你好", metadata={"token_budget": 100, "selected": {}}),
        workspace_path=None,
        project_id=None,
        preferred_profile_id=None,
        idempotency_key="budget",
        has_profiles=True,
    )
    assert "上下文窗口不足" in result.assistant_content
    model.assert_not_called()


def test_atomic_repository_reads_own_writes_and_rolls_back(service, source_root, work_root):
    with pytest.raises(RuntimeError, match="rollback"), service.repository.atomic():
        project = service.repository.create_project("temporary", [str(source_root)], str(work_root))
        assert service.repository.get_project(project.project_id).name == "temporary"
        assert len(service.repository.list_projects()) == 1
        raise RuntimeError("rollback")
    assert service.repository.list_projects() == []


def test_model_alff_draft_compiles_through_real_skill_validator_without_running(
    tmp_path, monkeypatch
):
    from neuroagent.application.work_draft import WorkDraft
    from neuroagent.bootstrap import build_service
    from tests.backend.test_environment import _fake_stack
    from tests.backend.test_science_agent_api import _minimal_preprocessing_payload

    source, work = tmp_path / "source", tmp_path / "work"
    subject = source / "FunRaw" / "sub-01"
    subject.mkdir(parents=True)
    work.mkdir()
    (subject / "rest.nii").write_bytes((348).to_bytes(4, "little") + b"\0" * 344)
    settings = _fake_stack(tmp_path / "fake", "preprocessing").model_copy(
        update={
            "database_url": f"sqlite:///{(tmp_path / 'dialogue.sqlite').as_posix()}",
            "allowed_source_roots": [source],
            "allowed_work_root": work,
            "enable_real_execution": False,
            "rag_db_dir": None,
        }
    )
    service = build_service(settings)
    values = _minimal_preprocessing_payload()
    values.pop("provenance")
    draft = WorkDraft(
        preprocessing=values,
        requested_metrics=["alff"],
        primary_outputs=["raw_alff"],
        study_protocol_ref="test:explicit-user-protocol",
        qc_mode="skip",
        alff_falff={
            "tr_seconds": 2.0,
            "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
            "requested_metrics": ["alff"],
            "requested_scalings": ["raw"],
            "mask_source": "dpabi_auto",
            "mask_artifact_id": None,
            "filter_timing": "disabled",
            "result_smoothing": False,
            "result_smoothing_fwhm_mm": None,
        },
    )
    configure(
        service,
        monkeypatch,
        [
            generated(
                "prepare_preprocessing_plan",
                draft.model_dump(
                    mode="json",
                    exclude={"confirmed_hash"},
                ),
            )
        ],
    )
    try:
        with TestClient(create_app(service=service)) as client:
            session = create(client, source)
            setup = turn(
                client,
                session,
                "登记并确认工作区",
                "setup-alff",
                action="setup_workspace",
                workspace_setup={
                    "project_name": "ALFF dialogue project",
                    "dataset_name": "ALFF input",
                    "work_root": str(work / "project"),
                    "selected_input_stage": "FunRaw",
                },
            )
            assert setup["tool_call"]["status"] == "succeeded"
            review = turn(client, session, "这是我明确选择的全部ALFF参数", "full-review")
            assert "work_draft" in review["assistant_message"]["payload"], review[
                "assistant_message"
            ]["content"]
            current_hash = review["assistant_message"]["payload"]["work_draft"]["hash"]
            result = turn(client, session, f"确认方案参数 {current_hash[:12]}", "compile-alff")
            payload = result["assistant_message"]["payload"]
            assert "plan_revision" in payload, result["assistant_message"]["content"]
            assert payload["plan_revision"]["state"] == "awaiting_approval"
            assert dict(payload["skill_plan"]["resolved_parameters"])["alff_falff"][
                "requested_metrics"
            ] == ["alff"]
            assert service.repository.list_runs() == []
    finally:
        service.close()


def _authorized_conversation(service: NeuroAgentService, *, work_draft: dict, key: str):
    """Seed a Work conversation that already holds a reviewed plan."""

    session = service.create_conversation(ConversationCreate(mode="work"), f"{key}-create")
    service.repository.append_conversation_exchange(
        session.conversation_id,
        user_content="prepare",
        assistant_content="review plan",
        assistant_payload={
            "plan_revision": {
                "plan_revision_id": "server-plan",
                "plan_hash": "b" * 64,
            }
        },
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=None,
        active_run_id=None,
    )
    service.repository.append_conversation_exchange(
        session.conversation_id,
        user_content="annotate draft",
        assistant_content="draft ready",
        assistant_payload={"work_draft": work_draft},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=None,
        active_run_id=None,
    )
    return service.get_conversation(session.conversation_id)


async def _send_confirmation(service: NeuroAgentService, session, key: str):
    from neuroagent.application.work_dialogue import WorkDialogue
    from neuroagent.context.interfaces import ContextPacket

    model = AsyncMock(side_effect=AssertionError("confirmation does not use model"))
    return await WorkDialogue(service.conversation_work, model).respond(
        session,
        ConversationTurnCreate(content="批准并启动 bbbbbbbbbbbb 跳过QC"),
        ContextPacket(question="confirmation"),
        workspace_path=None,
        project_id=None,
        preferred_profile_id=None,
        idempotency_key=key,
        has_profiles=True,
    )


@pytest.mark.asyncio
async def test_used_start_confirmation_cannot_be_replayed(service, monkeypatch):
    """A queued run must not be re-queued by resending the same command.

    Without single-use consumption the payload scan falls back to the earlier
    compiled draft, so the identical text would create a second run of the same
    plan -- and in ``in_place`` workspace mode two concurrent runs would write
    into one DPABI workspace.
    """

    session = _authorized_conversation(
        service,
        work_draft={
            "draft": {},
            "hash": "c" * 64,
            "workspace_path": None,
            "compiled": False,
            "consumed": True,
            "consumed_run_id": "run-1",
        },
        key="replay",
    )
    execute = Mock(side_effect=AssertionError("a used confirmation must not dispatch"))
    monkeypatch.setattr(service.conversation_work, "execute", execute)

    result = await _send_confirmation(service, session, "replay-confirm")

    execute.assert_not_called()
    assert result.commit is None
    # Legacy commands without a server-issued challenge are refused before any
    # coordinator dispatch, regardless of the old conversation marker.
    assert "确认" in result.assistant_content


@pytest.mark.asyncio
async def test_queued_start_records_single_use_confirmation(service, monkeypatch):
    session = _authorized_conversation(
        service,
        work_draft={
            "draft": {},
            "hash": "c" * 64,
            "workspace_path": None,
            "compiled": True,
        },
        key="consume",
    )
    execute = Mock(return_value=WorkActionResult("queued", active_run_id="run-7"))
    monkeypatch.setattr(service.conversation_work, "execute", execute)

    result = await _send_confirmation(service, session, "consume-confirm")
    assert result.commit is None
    assert execute.call_count == 0
    assert "确认" in result.assistant_content


@pytest.mark.asyncio
async def test_confirmation_stays_unused_when_nothing_was_queued(service, monkeypatch):
    session = _authorized_conversation(
        service,
        work_draft={
            "draft": {},
            "hash": "c" * 64,
            "workspace_path": None,
            "compiled": True,
        },
        key="unused",
    )
    # No run id means the request never reached the queue (e.g. the real
    # execution gate refused it), so the user may fix the cause and retry.
    execute = Mock(return_value=WorkActionResult("blocked"))
    monkeypatch.setattr(service.conversation_work, "execute", execute)

    result = await _send_confirmation(service, session, "unused-confirm")
    assert result.commit is None
    assert execute.call_count == 0
    assert "确认" in result.assistant_content


def test_prepare_plan_from_draft_requires_a_workspace(service):
    result = service.conversation_work.prepare_plan_from_draft(
        ConversationTurnCreate(content="确认方案参数"),
        complete_draft(),
        workspace_path=None,
        project_id=None,
        dataset_id=None,
        conversation_id="conv-noworkspace",
        user_confirmation_ref="conversation:conv-noworkspace:turn:2",
        idempotency_key="ppfd-noworkspace",
    )

    assert "请先选择工作区" in result.assistant_content
    assert result.payload == {}


def test_prepare_plan_from_draft_binds_registered_dataset_and_fails_closed(
    service, source_root, work_root
):
    """The dataset-bound branch must validate the workspace, then refuse a
    dataset that has no frozen manifest rather than compiling against nothing."""

    from neuroagent.application.contracts import DatasetCreate
    from tests.backend.conftest import make_bids_dataset, make_project

    dataset_root = make_bids_dataset(source_root)
    project = make_project(service, source_root, work_root, key="ppfd-project")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="unfrozen",
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        "ppfd-dataset",
    )

    result = service.conversation_work.prepare_plan_from_draft(
        ConversationTurnCreate(content="确认方案参数"),
        complete_draft(),
        workspace_path=str(dataset_root),
        project_id=project.project_id,
        dataset_id=dataset.dataset_id,
        conversation_id="conv-bound",
        user_confirmation_ref="conversation:conv-bound:turn:2",
        idempotency_key="ppfd-bound",
    )

    assert "方案尚未编译，也未运行" in result.assistant_content
    assert "plan_revision" not in result.payload
    assert service.repository.list_runs() == []


def test_file_citations_keep_evidence_from_multiple_reads_of_the_same_path():
    from neuroagent.application.contracts import WorkspaceCitation
    from neuroagent.application.work_dialogue import _valid_workspace_citation_paths

    path = "FunImg/subject-0123456789ab/scan.json"
    calls = [
        {
            "tool_name": "read_workspace_content",
            "status": "succeeded",
            "output": {"path": path, "locators": [{"line": 58, "json_pointer": "/SliceTiming"}]},
        },
        {
            "tool_name": "read_workspace_content",
            "status": "succeeded",
            "output": {"path": path, "locators": [{"line": 91}]},
        },
    ]
    citation = WorkspaceCitation(path=path, locator="/SliceTiming")
    assert _valid_workspace_citation_paths((citation,), {"calls": calls})
    assert not _valid_workspace_citation_paths(
        (WorkspaceCitation(path=path, locator="/Unobserved"),), {"calls": calls}
    )
