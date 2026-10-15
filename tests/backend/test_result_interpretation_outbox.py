# ruff: noqa: RUF001

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from neuroagent.agent.models import ModelProfile
from neuroagent.application.automation.models import (
    AuthoritativeRunSnapshot,
    AuthorityReferences,
    TaskBudget,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WaitingCondition,
    WaitingKind,
    WorkGoal,
    WorkTask,
)
from neuroagent.application.automation.result_interpretation import (
    ResultInterpretationStatus,
    StatisticalResultInterpretationOutbox,
    VerifiedResultInterpretationSource,
    project_result_model_facts,
)
from neuroagent.application.contracts.conversations import ConversationCreate, ConversationMode
from neuroagent.application.conversation_work import WorkActionResult
from neuroagent.context.engine import RegexTokenCounter
from tests.backend.conftest import make_project


def _ready_result_outbox(service, source_root, work_root):
    project = make_project(service, source_root, work_root, key="result-interpretation-project")
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode=ConversationMode.WORK, project_id=project.project_id),
        "result-interpretation-conversation",
    )
    outbox = StatisticalResultInterpretationOutbox(
        task_id="statistical-work-task-1",
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        run_id="statistical-run-1",
        plan_revision_id="statistical-plan-1",
        result_id="statistical-result-1",
        result_bundle_hash="a" * 64,
        result_version=1,
        artifact_ids=("registered-artifact-1",),
    )
    service.automation.ensure_result_interpretation_outbox(outbox)
    return project, conversation, outbox


def _verified_source(project, conversation, outbox):
    facts = project_result_model_facts(
        {
            "mode": "real",
            "non_scientific": False,
            "method": None,
            "inference_status": "no_significant_voxels",
            "significant_voxel_count": 0,
            "cluster_count": 0,
            "clusters": [],
            "localization_status": "no_significant_voxels",
            "interpretation_limits": ["零显著不等于等价。"],
        }
    )
    return VerifiedResultInterpretationSource(
        task_id=outbox.task_id,
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        run_id=outbox.run_id,
        plan_revision_id=outbox.plan_revision_id,
        result_id=outbox.result_id,
        result_bundle_hash=outbox.result_bundle_hash,
        result_version=outbox.result_version,
        artifact_ids=outbox.artifact_ids,
        source_evidence_hash="c" * 64,
        model_facts=facts,
        deterministic_readout="本次检验及校正阈值下未发现显著体素。",
    )


@pytest.mark.asyncio
async def test_verified_result_consumer_commits_assistant_only_and_deduplicates(
    service, source_root, work_root, monkeypatch
):
    project, conversation, outbox = _ready_result_outbox(service, source_root, work_root)
    service.repository.create_model_profile(
        ModelProfile(
            id="result-interpretation-model",
            provider="openai-compatible",
            base_url="https://example.org/v1",
            model="test-model",
            api_key_env="UNUSED_RESULT_INTERPRETATION_API_KEY",
            context_window_tokens=32768,
        )
    )
    facts = project_result_model_facts(
        {
            "mode": "real",
            "non_scientific": False,
            "method": {
                "test_label": "两样本 t 检验",
                "statistic_type": "T",
                "subject_count": 24,
                "image_count": 24,
                "group_counts": {"HC": 12, "MDD": 12},
                "condition_order": [],
                "covariates": [],
                "contrast": [1.0, -1.0],
                "tail_label": "双侧",
                "df1": 1,
                "df2": 22,
                "correction_method": "fdr",
                "assumptions": ["仅作方法核对"],
            },
            "inference_status": "no_significant_voxels",
            "significant_voxel_count": 0,
            "cluster_count": 0,
            "clusters": [],
            "localization_status": "no_significant_voxels",
            "interpretation_limits": ["零显著不等于等价。"],
        }
    )
    source = VerifiedResultInterpretationSource(
        task_id=outbox.task_id,
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        run_id=outbox.run_id,
        plan_revision_id=outbox.plan_revision_id,
        result_id=outbox.result_id,
        result_bundle_hash=outbox.result_bundle_hash,
        result_version=outbox.result_version,
        artifact_ids=outbox.artifact_ids,
        source_evidence_hash="b" * 64,
        model_facts=facts,
        deterministic_readout="本次检验和校正阈值下未发现显著体素；这不能证明组间等价。",
    )
    service.automation.verify_result_interpretation_source = lambda _outbox: source
    interpret = AsyncMock(
        return_value=(
            json.dumps(
                {
                    "answer": "本次校正后未发现显著体素，结果不支持作出等价性结论。",
                    "action": "conversation",
                }
            ),
            {"finish_reason": "stop"},
            None,
        )
    )
    monkeypatch.setattr(service.work_dialogue, "_interpret", interpret)
    before = service.repository.get_conversation(conversation.conversation_id)
    outcome = await service.conversations.consume_next_result_interpretation_outbox(
        "result-interpreter", lease_seconds=30
    )
    assert outcome == ResultInterpretationStatus.COMPLETED.value
    interpret.assert_awaited_once()
    context = interpret.await_args.args[0]
    assert context["work_context"]["proposal_only_result_interpretation"] is True
    assert "untrusted_content" in context["work_context"]["analysis_result_facts"]
    assert "task_id" not in json.dumps(context["work_context"]["analysis_result_facts"])
    assert RegexTokenCounter().count(context) < 5_000
    after = service.repository.get_conversation(conversation.conversation_id)
    assert len(after.messages) == len(before.messages) + 1
    assert after.messages[-1].role.value == "assistant"
    assert "statistical_result_interpretation" in after.messages[-1].payload
    assert not service.runs.list_runs(project_id=project.project_id)
    assert (
        service.automation.get_result_interpretation_outbox(outbox.task_id).status
        is ResultInterpretationStatus.COMPLETED
    )
    assert (
        await service.conversations.consume_next_result_interpretation_outbox(
            "result-interpreter", lease_seconds=30
        )
        is None
    )


@pytest.mark.asyncio
async def test_unverified_result_authority_fails_closed_without_model_call(
    service, source_root, work_root, monkeypatch
):
    project, conversation, outbox = _ready_result_outbox(service, source_root, work_root)
    service.repository.create_model_profile(
        ModelProfile(
            id="result-interpretation-model-fail-closed",
            provider="openai-compatible",
            base_url="https://example.org/v1",
            model="test-model",
            api_key_env="UNUSED_RESULT_INTERPRETATION_API_KEY",
            context_window_tokens=32768,
        )
    )
    from neuroagent.application.errors import ConflictError

    service.automation.verify_result_interpretation_source = lambda _outbox: (_ for _ in ()).throw(
        ConflictError("result_interpretation_result_not_scientific", "synthetic result")
    )
    interpret = AsyncMock()
    monkeypatch.setattr(service.work_dialogue, "_interpret", interpret)
    outcome = await service.conversations.consume_next_result_interpretation_outbox(
        "result-interpreter", lease_seconds=30
    )
    assert outcome == ResultInterpretationStatus.BLOCKED.value
    interpret.assert_not_awaited()
    conversation_after = service.repository.get_conversation(conversation.conversation_id)
    assert (
        conversation_after.messages[-1].payload["statistical_result_interpretation"]["status"]
        == "blocked"
    )
    assert (
        service.automation.get_result_interpretation_outbox(outbox.task_id).status
        is ResultInterpretationStatus.BLOCKED
    )
    assert not service.runs.list_runs(project_id=project.project_id)


@pytest.mark.asyncio
async def test_missing_model_profile_pauses_and_manual_retry_reuses_outbox_generation(
    service, source_root, work_root
):
    project, conversation, outbox = _ready_result_outbox(service, source_root, work_root)
    service.automation.verify_result_interpretation_source = lambda _outbox: _verified_source(
        project, conversation, outbox
    )
    outcome = await service.conversations.consume_next_result_interpretation_outbox(
        "result-interpreter", lease_seconds=30
    )
    assert outcome == ResultInterpretationStatus.WAITING_USER.value
    paused = service.automation.get_result_interpretation_outbox(outbox.task_id)
    assert paused.last_error_code == "result_interpretation_model_profile_missing"

    retried = service.conversations.retry_result_interpretation_outbox(
        conversation.conversation_id, outbox.task_id, "result-interpretation-retry"
    )
    recovered_after_lost_response = service.conversations.retry_result_interpretation_outbox(
        conversation.conversation_id, outbox.task_id, "result-interpretation-retry-recovery"
    )
    repeated = service.conversations.retry_result_interpretation_outbox(
        conversation.conversation_id, outbox.task_id, "result-interpretation-retry"
    )
    assert retried == repeated == recovered_after_lost_response
    ready = service.automation.get_result_interpretation_outbox(outbox.task_id)
    assert ready.status is ResultInterpretationStatus.READY
    assert ready.retry_generation == 1
    retry_messages = [
        message
        for message in repeated.messages
        if "statistical_result_interpretation_retry" in message.payload
    ]
    assert len(retry_messages) == 1


@pytest.mark.asyncio
async def test_post_model_authority_retries_pause_after_bounded_transient_failures(
    service, source_root, work_root, monkeypatch
):
    project, conversation, outbox = _ready_result_outbox(service, source_root, work_root)
    service.repository.create_model_profile(
        ModelProfile(
            id="result-interpretation-post-check-model",
            provider="openai-compatible",
            base_url="https://example.org/v1",
            model="test-model",
            api_key_env="UNUSED_RESULT_INTERPRETATION_API_KEY",
            context_window_tokens=32768,
        )
    )
    with service.database.session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE statistical_result_interpretation_outbox "
                "SET attempt_count=4 WHERE task_id=:task_id"
            ),
            {"task_id": outbox.task_id},
        )

    source = _verified_source(project, conversation, outbox)
    verify_calls = 0

    def verify(_outbox):
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 1:
            return source
        from neuroagent.application.errors import ConflictError

        raise ConflictError(
            "result_interpretation_authority_indeterminate",
            "statistical authority is changing",
        )

    service.automation.verify_result_interpretation_source = verify
    monkeypatch.setattr(
        service.work_dialogue,
        "prepare_statistical_result_interpretation",
        AsyncMock(
            return_value=WorkActionResult(
                "待审阅的统计结果解释草稿。",
                payload={},
                project_id=project.project_id,
            )
        ),
    )

    outcome = await service.conversations.consume_next_result_interpretation_outbox(
        "result-interpreter", lease_seconds=30
    )

    assert outcome == ResultInterpretationStatus.WAITING_USER.value
    paused = service.automation.get_result_interpretation_outbox(outbox.task_id)
    assert paused.status is ResultInterpretationStatus.WAITING_USER
    assert paused.last_error_code == "result_interpretation_retry_exhausted"
    latest = service.repository.get_conversation(conversation.conversation_id)
    assert (
        latest.messages[-1].payload["statistical_result_interpretation"]["retry_available"] is True
    )
    retried = service.conversations.retry_result_interpretation_outbox(
        conversation.conversation_id, outbox.task_id, "post-check-transient-retry"
    )
    assert (
        service.automation.get_result_interpretation_outbox(outbox.task_id).status
        is ResultInterpretationStatus.READY
    )
    assert (
        retried.messages[-1].payload["statistical_result_interpretation_retry"]["status"]
        == ResultInterpretationStatus.READY.value
    )


def test_statistical_task_completion_and_result_outbox_insert_share_rollback(
    service, source_root, work_root, monkeypatch
):
    _, conversation, outbox = _ready_result_outbox(service, source_root, work_root)
    task = WorkTask(
        task_id=outbox.task_id,
        goal=WorkGoal(
            description="Complete an approved statistical run",
            completion_conditions=("verified StatisticalResult is registered",),
        ),
        status=TaskStatus.WAITING_RUN,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.WORKFLOW_RUN,
            reference=outbox.run_id,
            reason="Wait for the approved run and result registration",
        ),
        authority_refs=AuthorityReferences(
            conversation_id=conversation.conversation_id,
            plan_revision_id=outbox.plan_revision_id,
            workflow_run_id=outbox.run_id,
            input_revision="draft-hash",
        ),
        steps=(
            TaskStep(
                step_id="statistical-run",
                sequence=0,
                kind="statistical_run",
                status=TaskStepStatus.WAITING,
                    authority_refs=AuthorityReferences(workflow_run_id=outbox.run_id),
            ),
        ),
        budget=TaskBudget(
            max_steps=5,
            max_model_calls=2,
            max_tokens=1000,
            max_elapsed_seconds=120,
        ),
    )
    service.automation._repository.create_task(task)
    snapshot = AuthoritativeRunSnapshot(
        run_id=outbox.run_id,
        run_state="succeeded",
        run_version=1,
        attempt=1,
        verified_statistical_result_id=outbox.result_id,
        result_artifact_ids=outbox.artifact_ids,
    )
    monkeypatch.setattr(
        service.automation,
        "_statistical_result_interpretation_record",
        lambda **_kwargs: outbox,
    )
    monkeypatch.setattr(
        service.automation._repository,
        "ensure_result_interpretation_outbox",
        lambda _record: (_ for _ in ()).throw(RuntimeError("injected outbox write failure")),
    )

    with pytest.raises(RuntimeError, match="injected outbox write failure"):
        service.automation._complete_statistical_task(task, outbox.run_id, (), snapshot)

    persisted = service.automation.get_task(task.task_id)
    assert persisted.status is TaskStatus.WAITING_RUN
    assert persisted.version == task.version
