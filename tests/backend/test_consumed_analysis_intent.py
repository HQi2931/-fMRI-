"""User decisions after automatic proposals retain their exact durable goal."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from neuroagent.application.automation.analysis_outbox import (
    AnalysisProposalOutbox,
    AnalysisSourceModelFacts,
    VerifiedAnalysisOutboxSource,
)
from neuroagent.application.automation.models import WorkTask
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationTurnCreate,
    ConversationWorkTaskView,
)
from neuroagent.application.errors import ConflictError
from neuroagent.application.hashing import content_hash
from neuroagent.application.work_dialogue import _consumed_analysis_intent
from tests.backend.test_analysis_outbox_consumer import add_model_profile
from tests.backend.test_work_analysis_handoffs import _source


def _interpret(patch):
    return (
        json.dumps(
            {
                "answer": "Review the scientific choices.",
                "action": "prepare_statistical_analysis",
                "analysis_patch": patch,
            }
        ),
        {"finish_reason": "stop"},
        None,
    )


def _seed(service, source_root, work_root, monkeypatch, *, complete):
    project, run, manifest, subjects, metrics, _ = _source(service, source_root, work_root)
    plan = service.plans.get_plan(run.plan_revision_id)
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id), "consumed-intent"
    )
    intent_id = "consumed-analysis-intent"
    original = "Analyze the ALFF maps after preprocessing."
    request_hash = content_hash(original)
    dependency = {
        "task_kind": "preprocess_then_analyze",
        "task_id": intent_id,
        "conversation_id": conversation.conversation_id,
        "project_id": project.project_id,
        "analysis_request": original,
    }
    service.repository.append_conversation_exchange(
        conversation.conversation_id,
        user_content=original,
        assistant_content="Original goal recorded.",
        assistant_payload={"agent_role": "preprocessing", "analysis_dependency": dependency},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=project.project_id,
        active_run_id=run.run_id,
    )
    refs = {
        "conversation_id": conversation.conversation_id,
        "analysis_intent_task_id": intent_id,
        "analysis_request_hash": request_hash,
        "plan_revision_id": plan.plan_revision_id,
        "workflow_run_id": run.run_id,
        "input_revision": manifest.content_hash,
    }
    repository = service.automation._repository
    repository.create_task(
        WorkTask.model_validate(
            {
                "task_id": "consumed-preprocessing-task",
                "goal": {"description": original, "completion_conditions": ["Fixture ready"]},
                "status": "completed",
                "authority_refs": refs,
                "steps": [
                    {
                        "step_id": "prep",
                        "sequence": 0,
                        "kind": "preprocessing_run",
                        "status": "succeeded",
                        "authority_refs": refs,
                    }
                ],
                "budget": {
                    "max_steps": 20,
                    "max_model_calls": 10,
                    "max_tokens": 100000,
                    "max_elapsed_seconds": 3600,
                },
            }
        )
    )
    outbox = AnalysisProposalOutbox(
        intent_task_id=intent_id,
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        preprocessing_task_id="consumed-preprocessing-task",
        plan_revision_id=plan.plan_revision_id,
        manifest_hash=manifest.content_hash,
        run_id=run.run_id,
        analysis_request_hash=request_hash,
    )
    repository.ensure_analysis_outbox(outbox)
    repository.mark_analysis_outbox_ready(
        intent_id,
        preprocessing_task_id=outbox.preprocessing_task_id,
        plan_revision_id=outbox.plan_revision_id,
        manifest_hash=outbox.manifest_hash,
        run_id=outbox.run_id,
        artifact_ids=tuple(item.artifact_id for item in metrics),
    )
    proof = service.statistics.prepare_work_analysis_handoff(run.run_id, project.project_id)
    mask_ids = tuple(item["artifact_id"] for item in proof.data_references["masks"])
    source = VerifiedAnalysisOutboxSource(
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        intent_task_id=intent_id,
        preprocessing_task_id=outbox.preprocessing_task_id,
        analysis_request=original,
        run_id=run.run_id,
        plan_revision_id=plan.plan_revision_id,
        manifest_hash=manifest.content_hash,
        artifact_ids=tuple(item.artifact_id for item in metrics),
        dependency_artifact_ids=mask_ids,
        source_evidence_hash="b" * 64,
        model_facts=AnalysisSourceModelFacts.summarize(
            source_run_state="succeeded",
            qc_status="approved",
            qc_review_state="approved",
            input_manifest_verified=True,
            metric_count=len(metrics),
            metric_artifact_types=(item.artifact_type for item in metrics),
            subject_count=len(subjects),
            mask_count=len(mask_ids),
            verified_artifact_count=len(metrics) + len(mask_ids),
        ),
    )
    monkeypatch.setattr(service.automation, "verify_analysis_outbox_source", lambda _record: source)
    add_model_profile(service)
    choices = {
        "test": "one_sample_t",
        "metric_artifact_type": "metric.synthetic.alff",
        "tail": "two_sided",
        "missing_value_policy": "error",
        "one_sample_baseline": 0,
    }
    if complete:
        choices.update(correction_method="fdr", correction_parameters={"q_threshold": 0.05})
    interpreter = AsyncMock(return_value=_interpret(choices))
    monkeypatch.setattr(service.work_dialogue, "_interpret", interpreter)
    outcome = asyncio.run(
        service.conversations.consume_next_analysis_outbox("consumed-test", lease_seconds=30)
    )
    assert outcome == ("proposal_ready" if complete else "waiting_user")
    return conversation.conversation_id, source, interpreter


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize(
    ("content", "agent_role"),
    [
        ("ALFF 用单样本 t 检验, 双侧 FDR; 阈值按指定值。", "analysis"),
        ("补充 ALFF, 调整阈值。", None),
    ],
)
def test_public_turn_keeps_consumed_goal_when_completing_or_revising_choices(
    service, source_root, work_root, monkeypatch, complete, content, agent_role
):
    conversation_id, source, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=complete
    )
    before = service.automation.get_composite_goal(conversation_id, source.intent_task_id)
    previous_task = before.analysis_work_task_id
    for index, threshold in enumerate((0.04, 0.03)):
        interpreter.return_value = _interpret(
            {
                "correction_method": "fdr",
                "correction_parameters": {"q_threshold": threshold},
                "analysis_intent_task_id": "model-invented-intent",
                "analysis_request_hash": "e" * 64,
                "source_run_id": "model-invented-run",
            }
        )
        response = asyncio.run(
            service.conversations.send_conversation_turn(
                conversation_id,
                ConversationTurnCreate(content=content, agent_role=agent_role),
                f"consumed-followup-{index}",
            )
        )
        payload = response.assistant_message.payload
        assert payload["agent_role"] == "analysis"
        assert interpreter.call_args.kwargs["execution_context"].allowed_run_ids == frozenset(
            {source.run_id}
        )
        assert payload["analysis_draft"]["analysis_intent_task_id"] == source.intent_task_id
        assert payload["analysis_draft"]["analysis_request_hash"] == content_hash(
            source.analysis_request
        )
        assert payload["analysis_intent_envelope"]["status"] == "consumed"
        assert payload["statistical_design_proposal"]["source_run_id"] == source.run_id
        goal = service.automation.get_composite_goal(conversation_id, source.intent_task_id)
        assert goal.analysis_work_task_id is not None
        assert goal.analysis_work_task_id != previous_task
        task = service.automation.get_task(goal.analysis_work_task_id)
        assert task.authority_refs.analysis_intent_task_id == source.intent_task_id
        assert task.authority_refs.analysis_request_hash == content_hash(source.analysis_request)
        assert task.status.value == "waiting_user"
        progress = service.automation.get_composite_goal_progress(
            conversation_id, source.intent_task_id
        )
        assert progress.reason_code == "analysis_user_decision"
        previous_task = task.task_id
    assert progress.analysis_summary == " ".join(source.analysis_request.split())
    assert len(service.runs.list_runs(project_id=source.project_id)) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "completed",
        "cleared",
        "different_intent",
        "hash_conflict",
        "request_conflict",
        "draft_hash",
        "role_hash",
        "external",
        "changed_run",
        "draft_source",
        "envelope_source",
        "continuation_source",
        "wrong_role",
    ],
)
def test_consumed_identity_fails_closed_on_changed_scope(
    service, source_root, work_root, monkeypatch, mutation
):
    conversation_id, source, _ = _seed(service, source_root, work_root, monkeypatch, complete=False)
    conversation = service.repository.get_conversation(conversation_id)
    request = ConversationTurnCreate(content="Use FDR correction.", agent_role="analysis")
    assert (
        _consumed_analysis_intent(conversation, request, project_id=source.project_id) is not None
    )
    # Modify the detached snapshot only; server records remain unchanged.
    conversation = conversation.model_copy(deep=True)
    payload = conversation.messages[-1].payload
    envelope = payload["analysis_intent_envelope"]
    if mutation in {"completed", "cleared"}:
        envelope["status"] = mutation
    elif mutation == "different_intent":
        envelope["task_id"] = "another-intent"
    elif mutation == "hash_conflict":
        envelope["analysis_request_hash"] = "d" * 64
    elif mutation == "request_conflict":
        original = next(
            message for message in conversation.messages if "analysis_dependency" in message.payload
        )
        original.payload["analysis_dependency"]["analysis_request"] = "Another goal"
    elif mutation == "draft_hash":
        payload["analysis_draft"]["draft"]["one_sample_baseline"] = 42
    elif mutation == "role_hash":
        conversation = conversation.model_copy(
            update={
                "work_tasks": (
                    ConversationWorkTaskView(agent_role="analysis", draft_hash="d" * 64),
                )
            }
        )
    elif mutation == "external":
        request = request.model_copy(update={"external_analysis_import_id": "external-source"})
    elif mutation == "changed_run":
        request = request.model_copy(update={"target_run_id": "another-run"})
    elif mutation == "draft_source":
        payload["analysis_draft"]["source_run_id"] = "another-run"
    elif mutation == "envelope_source":
        envelope["source_run_id"] = "another-run"
    elif mutation == "continuation_source":
        payload["analysis_outbox_continuation"]["run_id"] = "another-run"
    elif mutation == "wrong_role":
        conversation = conversation.model_copy(update={"foreground_agent_role": "preprocessing"})
    if mutation in {
        "different_intent",
        "hash_conflict",
        "request_conflict",
        "draft_hash",
        "role_hash",
        "draft_source",
        "envelope_source",
        "continuation_source",
    }:
        with pytest.raises(ConflictError) as failure:
            _consumed_analysis_intent(conversation, request, project_id=source.project_id)
        assert failure.value.code == "analysis_intent_resume_conflict"
    else:
        assert (
            _consumed_analysis_intent(conversation, request, project_id=source.project_id) is None
        )


def test_public_turn_rejects_consumed_source_conflict_before_model_or_writes(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=False
    )
    conversation = service.repository.get_conversation(conversation_id)
    envelope = dict(conversation.messages[-1].payload["analysis_intent_envelope"])
    envelope["source_run_id"] = "another-run"
    service.repository.append_conversation_exchange(
        conversation_id,
        user_content="Fixture source conflict.",
        assistant_content="Conflicting source evidence.",
        assistant_payload={"agent_role": "analysis", "analysis_intent_envelope": envelope},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=source.project_id,
        active_run_id=source.run_id,
    )
    before = service.repository.get_conversation(conversation_id)
    goal_before = service.automation.get_composite_goal(conversation_id, source.intent_task_id)
    interpreter.reset_mock()
    with pytest.raises(ConflictError) as failure:
        asyncio.run(
            service.conversations.send_conversation_turn(
                conversation_id,
                ConversationTurnCreate(content="Use FDR correction.", agent_role="analysis"),
                "consumed-source-conflict",
            )
        )
    assert failure.value.code == "analysis_intent_resume_conflict"
    interpreter.assert_not_called()
    assert service.repository.get_conversation(conversation_id) == before
    assert (
        service.automation.get_composite_goal(conversation_id, source.intent_task_id) == goal_before
    )
    assert len(service.runs.list_runs(project_id=source.project_id)) == 1


@pytest.mark.parametrize("interleaving", ["preprocessing_source", "other_goal_completed"])
def test_public_turn_uses_current_consumed_goal_despite_other_goal_updates(
    service, source_root, work_root, monkeypatch, interleaving
):
    conversation_id, source, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=False
    )
    if interleaving == "preprocessing_source":
        # The ordinary role-based resolver now points elsewhere. The consumed
        # goal must use its own verified source for both recovery and assembly.
        monkeypatch.setattr(
            "neuroagent.application.work_dialogue.preprocessing_source_run",
            lambda *_: "another-preprocessing-run",
        )
    else:
        service.repository.append_conversation_exchange(
            conversation_id,
            user_content="Fixture background completion.",
            assistant_content="Another goal completed.",
            assistant_payload={
                "agent_role": "analysis",
                "analysis_intent_envelope": {
                    "status": "completed",
                    "task_id": "other-analysis-goal",
                    "conversation_id": conversation_id,
                    "project_id": source.project_id,
                    "analysis_request_hash": "e" * 64,
                },
            },
            tool=None,
            workspace_path=None,
            preferred_profile_id=None,
            project_id=source.project_id,
            active_run_id=source.run_id,
        )
    interpreter.return_value = _interpret(
        {"correction_method": "fdr", "correction_parameters": {"q_threshold": 0.04}}
    )
    response = asyncio.run(
        service.conversations.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(content="Use FDR correction.", agent_role="analysis"),
            "interleaved-consumed-followup",
        )
    )
    payload = response.assistant_message.payload
    assert payload["analysis_draft"]["analysis_intent_task_id"] == source.intent_task_id
    assert payload["analysis_intent_envelope"]["task_id"] == source.intent_task_id
    assert payload["statistical_design_proposal"]["source_run_id"] == source.run_id
    goal = service.automation.get_composite_goal(conversation_id, source.intent_task_id)
    task = service.automation.get_task(goal.analysis_work_task_id)
    assert task.authority_refs.analysis_intent_task_id == source.intent_task_id
    assert len(service.runs.list_runs(project_id=source.project_id)) == 1
