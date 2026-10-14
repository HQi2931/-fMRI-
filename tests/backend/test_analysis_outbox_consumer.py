# ruff: noqa: RUF001

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import text

from neuroagent.agent.models import ModelProfile
from neuroagent.application.automation.analysis_outbox import (
    AnalysisOutboxStatus,
    AnalysisProposalOutbox,
    AnalysisSourceModelFacts,
    VerifiedAnalysisOutboxSource,
)
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationMode,
    ConversationRole,
)
from neuroagent.application.conversation_work import WorkActionResult
from neuroagent.application.errors import ConflictError
from neuroagent.application.hashing import content_hash
from neuroagent.application.statistics_draft import DesignIssue
from neuroagent.context.engine import RegexTokenCounter
from neuroagent.context.interfaces import ContextPacket
from tests.backend.conftest import make_project


def seed_ready_outbox(service, source_root, work_root):
    project = make_project(service, source_root, work_root, key="analysis-outbox-project")
    conversation = service.conversations.create_conversation(
        ConversationCreate(
            mode=ConversationMode.WORK,
            project_id=project.project_id,
        ),
        "analysis-outbox-conversation",
    )
    request = "先预处理，再比较两组 ALFF。"
    intent_id = "intent-outbox-consumer-1"
    intent = {
        "task_kind": "preprocess_then_analyze",
        "task_id": intent_id,
        "conversation_id": conversation.conversation_id,
        "project_id": project.project_id,
        "analysis_request": request,
        "status": "waiting_for_preprocessing",
    }
    service.repository.append_conversation_exchange(
        conversation.conversation_id,
        user_content=request,
        assistant_content="已记录复合任务意图。",
        assistant_payload={"analysis_dependency": intent, "agent_role": "preprocessing"},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=project.project_id,
        active_run_id=None,
    )
    outbox = AnalysisProposalOutbox(
        intent_task_id=intent_id,
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        preprocessing_task_id="preprocessing-task-1",
        plan_revision_id="plan-revision-1",
        manifest_hash="a" * 64,
        run_id="preprocessing-run-1",
        analysis_request_hash=content_hash(request),
    )
    repository = service.automation._repository
    repository.ensure_analysis_outbox(outbox)
    ready = repository.mark_analysis_outbox_ready(
        intent_id,
        preprocessing_task_id=outbox.preprocessing_task_id,
        plan_revision_id=outbox.plan_revision_id,
        manifest_hash=outbox.manifest_hash,
        run_id=outbox.run_id,
        artifact_ids=("target-metric",),
    )
    source = VerifiedAnalysisOutboxSource(
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        intent_task_id=intent_id,
        preprocessing_task_id=outbox.preprocessing_task_id,
        analysis_request=request,
        run_id=outbox.run_id,
        plan_revision_id=outbox.plan_revision_id,
        manifest_hash=outbox.manifest_hash,
        artifact_ids=("target-metric",),
        dependency_artifact_ids=("registered-mask",),
        source_evidence_hash="b" * 64,
        model_facts=AnalysisSourceModelFacts.summarize(
            source_run_state="succeeded",
            qc_status="approved",
            qc_review_state="approved",
            input_manifest_verified=True,
            metric_count=1,
            metric_artifact_types=("metric.alff.z_score",),
            subject_count=1,
            mask_count=1,
            verified_artifact_count=2,
        ),
    )
    proof = SimpleNamespace(
        status="ready",
        data_references={
            "metrics": [{"artifact_id": "target-metric"}],
            "masks": [{"artifact_id": "registered-mask"}],
        },
        evidence_hash="c" * 64,
    )
    service.automation.verify_analysis_outbox_source = lambda _outbox: source
    service.work_dialogue.prepare_analysis_handoff = lambda *_args, **_kwargs: proof
    return project, conversation, ready, source, proof


def add_model_profile(service) -> None:
    service.repository.create_model_profile(
        ModelProfile(
            id="outbox-test-model",
            provider="openai-compatible",
            base_url="https://example.org/v1",
            model="test-model",
            api_key_env="UNUSED_OUTBOX_TEST_API_KEY",
            context_window_tokens=32768,
        )
    )


@pytest.mark.asyncio
async def test_consumer_records_assistant_only_question_and_deduplicates(
    service, source_root, work_root, monkeypatch
):
    _, conversation, outbox, source, _ = seed_ready_outbox(service, source_root, work_root)
    source = source.model_copy(
        update={
            "model_facts": AnalysisSourceModelFacts.summarize(
                source_run_state="succeeded",
                qc_status="approved",
                qc_review_state="approved",
                input_manifest_verified=True,
                metric_count=300,
                metric_artifact_types=(
                    metric_type
                    for metric_type in (
                        "metric.alff.z_score",
                        "metric.falff.z_score",
                        "metric.reho.z_score",
                    )
                    for _ in range(100)
                ),
                subject_count=100,
                mask_count=1,
                verified_artifact_count=301,
            )
        }
    )
    service.automation.verify_analysis_outbox_source = lambda _outbox: source
    add_model_profile(service)
    interpret = AsyncMock(
        return_value=(
            json.dumps({"answer": "仍需研究者决定检验类型。", "action": "conversation"}),
            {"finish_reason": "stop"},
            None,
        )
    )
    monkeypatch.setattr(service.work_dialogue, "_interpret", interpret)
    before = service.repository.get_conversation(conversation.conversation_id)
    before_user_count = sum(message.role is ConversationRole.USER for message in before.messages)

    result = await service.conversations.consume_next_analysis_outbox(
        "outbox-worker", lease_seconds=30
    )

    assert result == AnalysisOutboxStatus.WAITING_USER.value
    interpret.assert_awaited_once()
    model_context = interpret.await_args.args[0]
    assert model_context["question"] == source.analysis_request
    work_context = model_context["work_context"]
    assert work_context["analysis_source_evidence"]["metric_count"] == 300
    assert work_context["analysis_source_evidence"]["metric_types"] == [
        "metric.alff.z_score",
        "metric.falff.z_score",
        "metric.reho.z_score",
    ]
    assert work_context["analysis_source_evidence"]["subject_count"] == 100
    assert "analysis_verified_artifact_ids" not in work_context
    assert "analysis_verified_target_artifact_ids" not in work_context
    assert "analysis_verified_dependency_artifact_ids" not in work_context
    assert "target-metric" not in json.dumps(work_context)
    assert "registered-mask" not in json.dumps(work_context)
    token_counter = RegexTokenCounter()
    assert token_counter.count(work_context["analysis_source_evidence"]) < 128
    assert token_counter.count(model_context) < 12_000

    after = service.repository.get_conversation(conversation.conversation_id)
    new_messages = after.messages[len(before.messages) :]
    assert len(new_messages) == 1
    assert new_messages[0].role is ConversationRole.ASSISTANT
    assert (
        sum(message.role is ConversationRole.USER for message in after.messages)
        == before_user_count
    )
    assert "检验类型尚未选择" in new_messages[0].content
    assert after.foreground_agent_role.value == "analysis"
    persisted = service.automation.get_analysis_outbox(outbox.intent_task_id)
    assert persisted.status is AnalysisOutboxStatus.WAITING_USER
    assert persisted.continuation_message_id == new_messages[0].message_id
    assert persisted.analysis_work_task_id is None
    assert (
        await service.conversations.consume_next_analysis_outbox("outbox-worker", lease_seconds=30)
        is None
    )
    assert len(service.repository.get_conversation(conversation.conversation_id).messages) == len(
        after.messages
    )
    assert service.runs.list_runs(project_id=conversation.project_id) == []


@pytest.mark.asyncio
async def test_statistical_assembler_receives_only_verified_targets_and_lineage_masks(
    service, source_root, work_root, monkeypatch
):
    _, conversation, _, source, _ = seed_ready_outbox(service, source_root, work_root)
    add_model_profile(service)
    captured = {}

    def assemble(
        *,
        draft,
        run_id,
        external_import_id=None,
        project_id=None,
        allowed_artifact_ids,
        verify_source_files=True,
    ):
        captured.update(
            draft=draft,
            run_id=run_id,
            external_import_id=external_import_id,
            project_id=project_id,
            allowed_artifact_ids=allowed_artifact_ids,
            verify_source_files=verify_source_files,
        )
        return None, (
            DesignIssue(
                code="statistics_mask_run_mismatch",
                message="Synthetic cross-run mask capability boundary.",
            ),
        )

    # The dialogue's registered callback is the production dry-run. Stub only
    # the shared proposal assembler here to expose the exact outbox allowlist
    # it receives; the internal lineage contract has its own real-source test.
    monkeypatch.setattr(service.statistics, "prepare_work_statistical_design_proposal", assemble)
    monkeypatch.setattr(
        service.work_dialogue,
        "_interpret",
        AsyncMock(
            return_value=(
                json.dumps(
                    {
                        "answer": "Prepare an explicit one-sample design draft.",
                        "action": "prepare_statistical_analysis",
                        "analysis_patch": {
                            "test": "one_sample_t",
                            "one_sample_baseline": 0,
                            "metric_artifact_type": "metric.synthetic.alff",
                            "tail": "two_sided",
                            "missing_value_policy": "error",
                            "correction_method": "none",
                        },
                    }
                ),
                {"finish_reason": "stop"},
                None,
            )
        ),
    )
    packet = ContextPacket(
        question=source.analysis_request,
        work_context={"analysis_verified_artifact_ids": ["forged-extra"]},
        metadata={"context_window_tokens": 16384, "max_output_tokens": 2048, "token_budget": 12000},
    )

    result = await service.work_dialogue.prepare_analysis_continuation(
        service.repository.get_conversation(conversation.conversation_id),
        packet,
        source,
        has_profiles=True,
    )

    assert captured["run_id"] == source.run_id
    assert captured["allowed_artifact_ids"] == frozenset({"target-metric", "registered-mask"})
    assert captured["draft"].correction_method == "none"
    assert captured["draft"].missing_value_policy.value == "error"
    assert "forged-extra" not in captured["allowed_artifact_ids"]
    assert result.payload["statistical_design_issues"] == [
        "Synthetic cross-run mask capability boundary."
    ]


@pytest.mark.asyncio
async def test_complete_proposal_binds_assistant_message_work_task_and_outbox_atomically(
    service, source_root, work_root
):
    _, conversation, outbox, _, _ = seed_ready_outbox(service, source_root, work_root)
    service.conversations.list_model_profiles = lambda: [object()]
    service.conversation_context.context_limits = lambda _profile_id: (16384, 2048)
    proposal_hash = "f" * 64
    draft_hash = "a" * 64
    continuation = AsyncMock(
        return_value=WorkActionResult(
            "服务端统计提案已准备好，等待你审查。",
            payload={
                "agent_role": "analysis",
                "analysis_outbox_continuation": {"intent_task_id": outbox.intent_task_id},
                "analysis_draft": {"draft": {}, "hash": draft_hash},
                "statistical_design_proposal": {
                    "proposal_hash": proposal_hash,
                    "project_id": conversation.project_id,
                    "source_run_id": outbox.run_id,
                    "input_manifest_hash": outbox.manifest_hash,
                    "metric_artifact_ids": ["target-metric"],
                    "design": {"test": "independent_two_sample_t"},
                },
            },
            project_id=conversation.project_id,
        )
    )
    service.work_dialogue.prepare_analysis_continuation = continuation
    before = service.repository.get_conversation(conversation.conversation_id)
    user_count = sum(item.role is ConversationRole.USER for item in before.messages)

    result = await service.conversations.consume_next_analysis_outbox(
        "outbox-worker", lease_seconds=30
    )

    assert result == AnalysisOutboxStatus.PROPOSAL_READY.value
    after = service.repository.get_conversation(conversation.conversation_id)
    new_messages = after.messages[len(before.messages) :]
    assert len(new_messages) == 1
    assistant = new_messages[0]
    assert assistant.role is ConversationRole.ASSISTANT
    assert assistant.payload["statistical_design_proposal"]["proposal_hash"] == proposal_hash
    assert sum(item.role is ConversationRole.USER for item in after.messages) == user_count
    analysis_binding = next(
        item for item in after.work_tasks if item.agent_role.value == "analysis"
    )
    persisted_outbox = service.automation.get_analysis_outbox(outbox.intent_task_id)
    assert analysis_binding.proposal_hash == proposal_hash
    assert analysis_binding.proposal_message_id == assistant.message_id
    assert analysis_binding.work_task_id == persisted_outbox.analysis_work_task_id
    assert persisted_outbox.status is AnalysisOutboxStatus.PROPOSAL_READY
    assert persisted_outbox.continuation_message_id == assistant.message_id
    assert persisted_outbox.analysis_work_task_id is not None
    task = service.automation.get_task(persisted_outbox.analysis_work_task_id)
    assert task.authority_refs.conversation_id == conversation.conversation_id
    assert task.authority_refs.input_revision == draft_hash
    assert task.steps[0].input_version == proposal_hash
    assert service.runs.list_runs(project_id=conversation.project_id) == []


@pytest.mark.asyncio
async def test_source_evidence_change_during_model_call_discards_generated_proposal(
    service, source_root, work_root
):
    _, conversation, outbox, source, proof = seed_ready_outbox(service, source_root, work_root)
    service.conversations.list_model_profiles = lambda: [object()]
    service.conversation_context.context_limits = lambda _profile_id: (16384, 2048)
    changed_source = source.model_copy(update={"source_evidence_hash": "d" * 64})
    verify_source = Mock(side_effect=[source, changed_source])
    service.automation.verify_analysis_outbox_source = verify_source
    changed_proof = SimpleNamespace(
        status="ready",
        data_references=proof.data_references,
        evidence_hash="e" * 64,
    )
    handoff = Mock(side_effect=[proof, changed_proof])
    service.work_dialogue.prepare_analysis_handoff = handoff
    continuation = AsyncMock(
        return_value=WorkActionResult(
            "generated proposal that must be discarded",
            payload={
                "agent_role": "analysis",
                "analysis_outbox_continuation": {"intent_task_id": outbox.intent_task_id},
                "analysis_draft": {"draft": {}, "hash": "b" * 64},
                "statistical_design_proposal": {
                    "proposal_hash": "c" * 64,
                    "project_id": conversation.project_id,
                    "design": {"test": "independent_two_sample_t"},
                },
            },
            project_id=conversation.project_id,
        )
    )
    service.work_dialogue.prepare_analysis_continuation = continuation
    before = service.repository.get_conversation(conversation.conversation_id)

    result = await service.conversations.consume_next_analysis_outbox(
        "outbox-worker", lease_seconds=30
    )

    assert result == AnalysisOutboxStatus.BLOCKED.value
    assert verify_source.call_count == 2
    assert handoff.call_count == 2
    after = service.repository.get_conversation(conversation.conversation_id)
    saved = after.messages[-1]
    assert saved.role is ConversationRole.ASSISTANT
    assert "statistical_design_proposal" not in saved.payload
    assert "analysis_draft" not in saved.payload
    assert "c" * 64 not in json.dumps(saved.payload)
    assert len(after.work_tasks) == len(before.work_tasks)
    persisted = service.automation.get_analysis_outbox(outbox.intent_task_id)
    assert persisted.last_error_code == "analysis_outbox_source_changed_during_generation"
    assert persisted.analysis_work_task_id is None
    with service.database.session_factory() as session:
        assert session.execute(text("SELECT COUNT(*) FROM work_tasks")).scalar_one() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_code",
    [
        "analysis_outbox_qc_or_artifact_mismatch",
        "analysis_outbox_artifact_ids_mismatch",
    ],
)
async def test_unapproved_qc_or_unregistered_artifact_is_blocked_before_model(
    service, source_root, work_root, error_code
):
    _, conversation, outbox, _, _ = seed_ready_outbox(service, source_root, work_root)
    verify_source = Mock(
        side_effect=ConflictError(error_code, "synthetic source verification failure")
    )
    service.automation.verify_analysis_outbox_source = verify_source
    handoff = Mock(side_effect=AssertionError("handoff must not run for unverified sources"))
    service.work_dialogue.prepare_analysis_handoff = handoff
    model = AsyncMock(side_effect=AssertionError("model must not run for unverified sources"))
    service.work_dialogue.prepare_analysis_continuation = model

    result = await service.conversations.consume_next_analysis_outbox(
        "outbox-worker", lease_seconds=30
    )

    assert result == AnalysisOutboxStatus.BLOCKED.value
    verify_source.assert_called_once()
    handoff.assert_not_called()
    model.assert_not_awaited()
    after = service.repository.get_conversation(conversation.conversation_id)
    assert after.messages[-1].role is ConversationRole.ASSISTANT
    assert (
        service.automation.get_analysis_outbox(outbox.intent_task_id).last_error_code == error_code
    )


@pytest.mark.asyncio
async def test_missing_profile_waits_for_user_and_manual_retry_is_idempotent(
    service, source_root, work_root
):
    _, conversation, outbox, _, _ = seed_ready_outbox(service, source_root, work_root)
    service.conversations.list_model_profiles = lambda: []
    continuation = AsyncMock()
    service.work_dialogue.prepare_analysis_continuation = continuation

    result = await service.conversations.consume_next_analysis_outbox(
        "outbox-worker", lease_seconds=30
    )

    assert result == AnalysisOutboxStatus.WAITING_USER.value
    continuation.assert_not_awaited()
    waiting = service.automation.get_analysis_outbox(outbox.intent_task_id)
    assert waiting.last_error_code == "analysis_model_profile_missing"
    assert waiting.continuation_message_id is not None
    before = service.repository.get_conversation(conversation.conversation_id)
    message = before.messages[-1]
    assert message.role is ConversationRole.ASSISTANT
    assert message.payload["analysis_outbox_result"]["retry_available"] is True

    retried = service.conversations.retry_analysis_outbox(
        conversation.conversation_id, outbox.intent_task_id, "manual-retry-key"
    )
    repeated = service.conversations.retry_analysis_outbox(
        conversation.conversation_id, outbox.intent_task_id, "manual-retry-key"
    )
    assert retried == repeated
    after = service.repository.get_conversation(conversation.conversation_id)
    assert len(after.messages) == len(before.messages) + 1
    assert after.messages[-1].role is ConversationRole.ASSISTANT
    assert after.messages[-1].payload["analysis_outbox_retry"]["retry_generation"] == 1
    assert sum(item.role is ConversationRole.USER for item in after.messages) == sum(
        item.role is ConversationRole.USER for item in before.messages
    )
    ready = service.automation.get_analysis_outbox(outbox.intent_task_id)
    assert ready.status is AnalysisOutboxStatus.READY
    assert ready.retry_generation == 1
    assert ready.attempt_count == 0


@pytest.mark.asyncio
async def test_transient_model_errors_back_off_and_stop_after_five_attempts(
    service, source_root, work_root, monkeypatch
):
    import neuroagent.infrastructure.persistence.automation_repository as automation_repository

    _, _, outbox, source, _ = seed_ready_outbox(service, source_root, work_root)
    now = [1000.0]
    monkeypatch.setattr(automation_repository.time, "time", lambda: now[0])
    service.conversations.list_model_profiles = lambda: [object()]
    service.conversation_context.context_limits = lambda _profile_id: (16384, 2048)
    continuation = AsyncMock(
        return_value=WorkActionResult(
            "model unavailable",
            payload={"model_error": {"code": "provider_unavailable"}},
        )
    )
    service.work_dialogue.prepare_analysis_continuation = continuation

    results = []
    for _ in range(5):
        results.append(
            await service.conversations.consume_next_analysis_outbox(
                "outbox-worker", lease_seconds=30
            )
        )
        now[0] += 1000

    assert results == ["deferred", "deferred", "deferred", "deferred", "waiting_user"]
    assert continuation.await_count == 5
    assert continuation.await_args.args[2].analysis_request == source.analysis_request
    final = service.automation.get_analysis_outbox(outbox.intent_task_id)
    assert final.status is AnalysisOutboxStatus.WAITING_USER
    assert final.attempt_count == 5
    assert final.last_error_code == "analysis_model_retry_exhausted"
    conversation = service.repository.get_conversation(outbox.conversation_id)
    continuations = [
        item
        for item in conversation.messages
        if item.payload.get("analysis_outbox_continuation", {}).get("intent_task_id")
        == outbox.intent_task_id
    ]
    assert len(continuations) == 1
    assert continuations[0].role is ConversationRole.ASSISTANT


@pytest.mark.asyncio
async def test_expired_fence_rolls_back_assistant_message_and_analysis_work_task(
    service, source_root, work_root, monkeypatch
):
    _, conversation, outbox, _, _ = seed_ready_outbox(service, source_root, work_root)
    service.conversations.list_model_profiles = lambda: [object()]
    service.conversation_context.context_limits = lambda _profile_id: (16384, 2048)
    proposal_hash = "d" * 64
    draft_hash = "e" * 64

    async def stale_claim(_conversation, _packet, source, *, has_profiles):
        assert has_profiles
        with service.database.session_factory.begin() as session:
            session.execute(
                text(
                    "UPDATE analysis_proposal_outbox SET lease_owner='new-worker', "
                    "fencing_token=fencing_token+1, lease_expires_at=:expires "
                    "WHERE intent_task_id=:intent_id"
                ),
                {"expires": time.time() + 600, "intent_id": source.intent_task_id},
            )
        return WorkActionResult(
            "review this statistical proposal",
            payload={
                "agent_role": "analysis",
                "analysis_outbox_continuation": {"intent_task_id": source.intent_task_id},
                "analysis_draft": {"draft": {}, "hash": draft_hash},
                "statistical_design_proposal": {
                    "proposal_hash": proposal_hash,
                    "project_id": source.project_id,
                    "design": {"test": "independent_two_sample_t"},
                },
            },
            project_id=source.project_id,
        )

    monkeypatch.setattr(service.work_dialogue, "prepare_analysis_continuation", stale_claim)
    before = service.repository.get_conversation(conversation.conversation_id)

    result = await service.conversations.consume_next_analysis_outbox(
        "outbox-worker", lease_seconds=30
    )

    assert result == "claim_lost"
    after = service.repository.get_conversation(conversation.conversation_id)
    assert after.messages == before.messages
    assert after.work_tasks == before.work_tasks
    persisted = service.automation.get_analysis_outbox(outbox.intent_task_id)
    assert persisted.status is AnalysisOutboxStatus.READY
    assert persisted.continuation_message_id is None
    assert persisted.analysis_work_task_id is None
    assert persisted.lease_owner == "new-worker"
    with service.database.session_factory() as session:
        task_count = session.execute(text("SELECT COUNT(*) FROM work_tasks")).scalar_one()
    assert task_count == 0


@pytest.mark.asyncio
async def test_conversation_project_switch_is_terminal_and_does_not_mutate_work_state(
    service, source_root, work_root
):
    _project, conversation, outbox, _, _ = seed_ready_outbox(service, source_root, work_root)
    other_project = make_project(service, source_root, work_root, key="other-project")
    with service.database.session_factory.begin() as session:
        session.execute(
            text("UPDATE conversations SET project_id=:project_id WHERE conversation_id=:id"),
            {"project_id": other_project.project_id, "id": conversation.conversation_id},
        )
    before = service.repository.get_conversation(conversation.conversation_id)
    verify_source = Mock(side_effect=AssertionError("source must not be read"))
    service.automation.verify_analysis_outbox_source = verify_source
    proof = service.work_dialogue.prepare_analysis_handoff

    result = await service.conversations.consume_next_analysis_outbox(
        "outbox-worker", lease_seconds=30
    )

    assert result == AnalysisOutboxStatus.BLOCKED.value
    verify_source.assert_not_called()
    assert service.work_dialogue.prepare_analysis_handoff is proof
    after = service.repository.get_conversation(conversation.conversation_id)
    assert after.project_id == other_project.project_id
    assert after.work_tasks == before.work_tasks
    assert after.foreground_agent_role == before.foreground_agent_role
    assert after.active_run_id == before.active_run_id
    assert sum(item.role is ConversationRole.USER for item in after.messages) == sum(
        item.role is ConversationRole.USER for item in before.messages
    )
    notice = after.messages[-1]
    assert notice.role is ConversationRole.ASSISTANT
    assert outbox.intent_task_id not in json.dumps(notice.payload)
    assert outbox.run_id not in json.dumps(notice.payload)
    assert "target-metric" not in json.dumps(notice.payload)
    assert (
        service.automation.get_analysis_outbox(outbox.intent_task_id).status
        is AnalysisOutboxStatus.BLOCKED
    )
