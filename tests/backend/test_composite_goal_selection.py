"""Goal selection restores exact review context without executing scientific work."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.automation.analysis_outbox import AnalysisProposalOutbox
from neuroagent.application.contracts.conversations import (
    CompositeGoalSelection,
    ConversationCreate,
    ConversationTurnCreate,
    WorkAgentSelection,
)
from neuroagent.application.contracts.runs import RunCreate
from neuroagent.application.contracts.statistics import (
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticalWorkContext,
    StatisticsRunCreate,
)
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.statistics_draft import StatisticsDraft, statistics_draft_hash
from neuroagent.application.work_agents import (
    latest_agent_payload,
    latest_analysis_dependency,
    latest_handoff_for_agent,
    recent_messages_for_agent,
)
from neuroagent.application.work_tasks import preprocessing_source_run, work_task
from neuroagent.multi_agent.registry import WorkAgentRole
from tests.backend.test_consumed_analysis_intent import _interpret, _seed
from tests.backend.test_work_analysis_handoffs import _database_table_counts
from tests.backend.test_work_task_bindings import _approve


def _select(service, conversation_id, intent_id, key, *, version=None):
    conversation = service.conversations.get_conversation(conversation_id)
    return service.conversations.select_composite_goal(
        conversation_id,
        intent_id,
        CompositeGoalSelection(expected_version=version or conversation.version),
        key,
    )


def _message_for(conversation, intent_id):
    return next(
        message
        for message in reversed(conversation.messages)
        if message.payload.get("analysis_draft", {}).get("analysis_intent_task_id") == intent_id
    )


def _second_goal(
    service, conversation_id, first_source, interpreter, monkeypatch, *, q_threshold=0.04
):
    original = service.automation.get_analysis_outbox(first_source.intent_task_id)
    source = first_source.model_copy(update={"intent_task_id": "second-analysis-intent"})
    outbox = AnalysisProposalOutbox(
        intent_task_id=source.intent_task_id,
        conversation_id=conversation_id,
        project_id=source.project_id,
        preprocessing_task_id=source.preprocessing_task_id,
        plan_revision_id=source.plan_revision_id,
        manifest_hash=source.manifest_hash,
        run_id=source.run_id,
        analysis_request_hash=original.analysis_request_hash,
    )
    repository = service.automation._repository
    service.repository.append_assistant_continuation(
        conversation_id,
        continuation_key="second-analysis-goal-intent",
        assistant_content="A second independent analysis request was recorded.",
        assistant_payload={
            "agent_role": "analysis",
            "analysis_dependency": {
                "task_kind": "preprocess_then_analyze",
                "task_id": source.intent_task_id,
                "conversation_id": conversation_id,
                "project_id": source.project_id,
                "analysis_request": source.analysis_request,
            },
        },
        project_id=source.project_id,
        work_task_id=None,
        update_work_state=False,
    )
    repository.ensure_analysis_outbox(outbox)
    repository.mark_analysis_outbox_ready(
        source.intent_task_id,
        preprocessing_task_id=source.preprocessing_task_id,
        plan_revision_id=source.plan_revision_id,
        manifest_hash=source.manifest_hash,
        run_id=source.run_id,
        artifact_ids=source.artifact_ids,
    )
    interpreter.return_value = _interpret(
        {
            "test": "one_sample_t",
            "metric_artifact_type": "metric.synthetic.alff",
            "tail": "two_sided",
            "missing_value_policy": "error",
            "one_sample_baseline": 0,
            "correction_method": "fdr",
            "correction_parameters": {"q_threshold": q_threshold},
        }
    )
    monkeypatch.setattr(service.automation, "verify_analysis_outbox_source", lambda _record: source)
    assert (
        asyncio.run(
            service.conversations.consume_next_analysis_outbox(
                "second-goal-worker", lease_seconds=30
            )
        )
        == "proposal_ready"
    )
    return source


def _design_request(conversation, intent_id, *, message_id=None):
    message = _message_for(conversation, intent_id)
    proposal = message.payload["statistical_design_proposal"]
    return StatisticalDesignCreate(
        project_id=proposal["project_id"],
        expected_project_version=proposal["project_version"],
        input_manifest_hash=proposal["input_manifest_hash"],
        design=proposal["design"],
        correction=proposal["correction"],
        work_context=StatisticalWorkContext(
            conversation_id=conversation.conversation_id,
            proposal_hash=proposal["proposal_hash"],
            proposal_message_id=message_id or message.message_id,
        ),
    )


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("transport", ["service", "api"])
def test_select_restores_exact_draft_and_original_proposal_message(
    service, source_root, work_root, monkeypatch, complete, transport
):
    conversation_id, source, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=complete
    )
    before = service.conversations.get_conversation(conversation_id)
    original = _message_for(before, source.intent_task_id)
    interpreter.reset_mock()
    if transport == "service":
        selected = _select(service, conversation_id, source.intent_task_id, "select-exact")
    else:
        with TestClient(create_app(service=service)) as client:
            response = client.post(
                f"/api/v1/conversations/{conversation_id}/composite-goals/"
                f"{source.intent_task_id}/select",
                headers={"Idempotency-Key": "select-exact"},
                json={"expected_version": before.version},
            )
            assert response.status_code == 200, response.text
        selected = service.conversations.get_conversation(conversation_id)
    task = work_task(selected, WorkAgentRole.ANALYSIS)
    assert task.analysis_intent_task_id == source.intent_task_id
    assert task.draft_hash == original.payload["analysis_draft"]["hash"]
    assert task.proposal_message_id == (original.message_id if complete else None)
    assert task.proposal_hash == (
        original.payload["statistical_design_proposal"]["proposal_hash"] if complete else None
    )
    assert selected.foreground_agent_role is WorkAgentRole.ANALYSIS
    assert selected.active_run_id is None
    assert selected.messages == before.messages
    assert selected.version == before.version + 1
    interpreter.assert_not_called()
    assert len(service.runs.list_runs(project_id=source.project_id)) == 1


def test_selection_replay_does_not_switch_back_after_selecting_another_goal(
    service, source_root, work_root, monkeypatch
):
    conversation_id, first, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=True
    )
    second = _second_goal(service, conversation_id, first, interpreter, monkeypatch)
    version = service.conversations.get_conversation(conversation_id).version
    first_selected = _select(
        service, conversation_id, first.intent_task_id, "select-first", version=version
    )
    second_selected = _select(service, conversation_id, second.intent_task_id, "select-second")
    replay = _select(
        service, conversation_id, first.intent_task_id, "select-first", version=version
    )
    assert replay == first_selected
    assert service.conversations.get_conversation(conversation_id) == second_selected
    assert work_task(second_selected, WorkAgentRole.ANALYSIS).analysis_intent_task_id == (
        second.intent_task_id
    )


def test_selection_rejects_stale_version_and_cross_conversation_without_writes(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, _ = _seed(service, source_root, work_root, monkeypatch, complete=True)
    before = service.conversations.get_conversation(conversation_id)
    with pytest.raises(ConflictError):
        _select(
            service,
            conversation_id,
            source.intent_task_id,
            "select-stale",
            version=before.version - 1,
        )
    assert service.conversations.get_conversation(conversation_id) == before
    other = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=source.project_id), "selection-other-chat"
    )
    with pytest.raises(NotFoundError):
        _select(service, other.conversation_id, source.intent_task_id, "select-cross-chat")
    assert service.conversations.get_conversation(other.conversation_id) == other


def test_new_incomplete_revision_cannot_restore_older_proposal(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, _ = _seed(service, source_root, work_root, monkeypatch, complete=True)
    conversation = service.conversations.get_conversation(conversation_id)
    payload = deepcopy(_message_for(conversation, source.intent_task_id).payload)
    payload.pop("statistical_design_proposal")
    saved = payload["analysis_draft"]
    draft = StatisticsDraft.model_validate(saved["draft"]).model_copy(
        update={"correction_method": None, "correction_parameters": {}}
    )
    saved["draft"] = draft.model_dump(mode="json")
    saved["hash"] = statistics_draft_hash(draft)
    service.repository.append_assistant_continuation(
        conversation_id,
        continuation_key="incomplete-analysis-revision",
        assistant_content="The newer draft still requires a correction decision.",
        assistant_payload=payload,
        project_id=source.project_id,
        work_task_id=None,
        update_work_state=False,
    )
    selected = _select(service, conversation_id, source.intent_task_id, "select-incomplete")
    task = work_task(selected, WorkAgentRole.ANALYSIS)
    assert task.draft_hash == saved["hash"]
    assert task.proposal_hash is None
    assert task.proposal_message_id is None
    assert task.work_task_id is None
    assert task.plan_revision_id is None
    assert task.run_id is None


@pytest.mark.parametrize("phase", ["plan", "run"])
@pytest.mark.parametrize("transport", ["service", "api"])
def test_goal_selection_restores_formal_plan_or_run_context(
    service, source_root, work_root, monkeypatch, phase, transport
):
    conversation_id, source, _ = _seed(service, source_root, work_root, monkeypatch, complete=True)
    conversation = _select(service, conversation_id, source.intent_task_id, "select-before-plan")
    frozen = service.statistics.create_statistical_design(
        _design_request(conversation, source.intent_task_id), "select-freeze-plan"
    )
    if phase == "run":
        plan = frozen.plan_revision
        service.statistics.validate_statistical_design(
            plan.plan_revision_id,
            StatisticalDesignValidationRequest(expected_version=plan.version),
            "select-validate-plan",
        )
        _approve(service, plan.plan_revision_id)
        service.runs.create_statistics_run(
            StatisticsRunCreate(
                project_id=source.project_id,
                statistical_design_revision_id=plan.plan_revision_id,
                expected_plan_hash=plan.plan_hash,
            ),
            "select-mock-run",
        )
    before = service.conversations.get_conversation(conversation_id)
    with TestClient(create_app(service=service)) as client:
        progress = client.get(f"/api/v1/conversations/{conversation_id}/composite-goals")
        assert progress.status_code == 200, progress.text
        goal = next(
            item for item in progress.json() if item["intent_task_id"] == source.intent_task_id
        )
        assert goal["status"] == ("waiting_review" if phase == "plan" else "waiting_run")
        assert goal["analysis_selection_available"] is True
        if transport == "api":
            response = client.post(
                f"/api/v1/conversations/{conversation_id}/composite-goals/"
                f"{source.intent_task_id}/select",
                headers={"Idempotency-Key": "select-formal-context"},
                json={"expected_version": before.version},
            )
            assert response.status_code == 200, response.text
    selected = (
        service.conversations.get_conversation(conversation_id)
        if transport == "api"
        else _select(service, conversation_id, source.intent_task_id, "select-formal-context")
    )
    assert work_task(selected, WorkAgentRole.ANALYSIS) == work_task(before, WorkAgentRole.ANALYSIS)
    assert selected.active_run_id == work_task(before, WorkAgentRole.ANALYSIS).run_id
    assert selected.messages == before.messages


def test_background_goal_continuation_preserves_selected_slot_and_foreground(
    service, source_root, work_root, monkeypatch
):
    conversation_id, first, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=True
    )
    selected = _select(service, conversation_id, first.intent_task_id, "select-background-owner")
    before = service.conversations.select_work_agent(
        conversation_id,
        WorkAgentSelection(agent_role="preprocessing", expected_version=selected.version),
        "foreground-preprocessing",
    )
    second = _second_goal(service, conversation_id, first, interpreter, monkeypatch)
    after = service.conversations.get_conversation(conversation_id)
    assert work_task(after, WorkAgentRole.ANALYSIS) == work_task(before, WorkAgentRole.ANALYSIS)
    assert after.foreground_agent_role == before.foreground_agent_role
    assert after.active_run_id == before.active_run_id
    assert _message_for(after, second.intent_task_id).payload["statistical_design_proposal"]
    assert (
        latest_agent_payload(after, "analysis_draft", WorkAgentRole.ANALYSIS)
        == (_message_for(before, first.intent_task_id).payload["analysis_draft"])
    )
    assert recent_messages_for_agent(after, WorkAgentRole.ANALYSIS) == recent_messages_for_agent(
        before, WorkAgentRole.ANALYSIS
    )


def test_same_hash_with_different_proposal_message_is_rejected(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, _ = _seed(service, source_root, work_root, monkeypatch, complete=True)
    original = _message_for(
        service.conversations.get_conversation(conversation_id), source.intent_task_id
    )
    _, duplicate = service.repository.append_assistant_continuation(
        conversation_id,
        continuation_key="same-hash-new-proposal-message",
        assistant_content="Repeated proposal with the same hash.",
        assistant_payload=deepcopy(original.payload),
        project_id=source.project_id,
        work_task_id=None,
        update_work_state=False,
    )
    selected = _select(service, conversation_id, source.intent_task_id, "select-message-owner")
    assert work_task(selected, WorkAgentRole.ANALYSIS).proposal_message_id == duplicate.message_id
    with pytest.raises(ConflictError) as failure:
        service.statistics.create_statistical_design(
            _design_request(selected, source.intent_task_id, message_id=original.message_id),
            "select-wrong-message",
        )
    assert failure.value.code == "statistics_work_proposal_stale"
    assert service.conversations.get_conversation(conversation_id) == selected


def test_selection_event_failure_rolls_back_pointer_and_allows_same_key_retry(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, _ = _seed(service, source_root, work_root, monkeypatch, complete=True)
    before = service.conversations.get_conversation(conversation_id)
    original_append = service.conversations.event_store.append_event

    def fail_selection_event(**kwargs):
        if kwargs.get("event_type") == "CompositeAnalysisGoalSelected":
            raise RuntimeError("Synthetic selection event failure")
        return original_append(**kwargs)

    monkeypatch.setattr(service.conversations.event_store, "append_event", fail_selection_event)
    with pytest.raises(RuntimeError, match="Synthetic selection event failure"):
        _select(service, conversation_id, source.intent_task_id, "select-rollback")
    assert service.conversations.get_conversation(conversation_id) == before
    monkeypatch.setattr(service.conversations.event_store, "append_event", original_append)
    selected = _select(service, conversation_id, source.intent_task_id, "select-rollback")
    assert selected.version == before.version + 1
    assert selected.messages == before.messages


@pytest.mark.parametrize("background_status", ["active", "cleared"])
def test_other_intent_or_unscoped_envelope_cannot_replace_selected_goal_reply(
    service, source_root, work_root, monkeypatch, background_status
):
    conversation_id, source, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=True
    )
    selected = _select(service, conversation_id, source.intent_task_id, "select-before-envelope")
    original = _message_for(selected, source.intent_task_id)
    proof = service.statistics.prepare_work_analysis_handoff(source.run_id, source.project_id)
    service.repository.append_assistant_continuation(
        conversation_id,
        continuation_key="selected-goal-handoff",
        assistant_content="Selected source evidence.",
        assistant_payload={
            "agent_role": "analysis",
            "analysis_intent_envelope": deepcopy(original.payload["analysis_intent_envelope"]),
            "agent_handoff": proof.model_dump(mode="json"),
        },
        project_id=source.project_id,
        work_task_id=None,
        update_work_state=False,
    )
    envelope = {
        "status": background_status,
        "conversation_id": conversation_id,
        "project_id": source.project_id,
    }
    if background_status == "active":
        envelope.update(task_id="background-intent", analysis_request_hash="c" * 64)
    service.repository.append_assistant_continuation(
        conversation_id,
        continuation_key=f"background-{background_status}-envelope",
        assistant_content="Background envelope update.",
        assistant_payload={
            "agent_role": "analysis",
            "analysis_intent_envelope": envelope,
            "analysis_dependency": {
                "task_id": "background-intent",
                "project_id": source.project_id,
                "analysis_request": "A different background request.",
            },
            "agent_handoff": {
                "to_agent": "analysis",
                "status": "blocked",
                "data_references": {"project_id": source.project_id},
            },
        },
        project_id=source.project_id,
        work_task_id=None,
        update_work_state=False,
    )
    before = service.conversations.get_conversation(conversation_id)
    assert latest_analysis_dependency(before)["task_id"] == source.intent_task_id
    assert latest_handoff_for_agent(before, WorkAgentRole.ANALYSIS) == proof.model_dump(mode="json")
    interpreter.return_value = _interpret(
        {"correction_method": "fdr", "correction_parameters": {"q_threshold": 0.03}}
    )
    response = asyncio.run(
        service.conversations.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(content="Use FDR correction.", agent_role="analysis"),
            "selected-envelope-followup",
        )
    )
    payload = response.assistant_message.payload
    assert payload["analysis_draft"]["analysis_intent_task_id"] == source.intent_task_id
    assert payload["analysis_intent_envelope"]["task_id"] == source.intent_task_id
    assert payload["analysis_dependency"]["task_id"] == source.intent_task_id


@pytest.mark.parametrize("agent_role", [None, "analysis"])
def test_selected_goal_source_overrides_other_preprocessing_slot_and_packet(
    service, source_root, work_root, monkeypatch, agent_role
):
    conversation_id, source, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=True
    )
    plan = service.plans.get_plan(source.plan_revision_id)
    other_run = service.runs.create_run(
        RunCreate(
            project_id=source.project_id,
            plan_revision_id=plan.plan_revision_id,
            expected_plan_hash=plan.plan_hash,
        ),
        "other-preprocessing-slot-run",
    )
    service.repository.append_conversation_exchange(
        conversation_id,
        user_content="A separate preprocessing task.",
        assistant_content="Other preprocessing run queued.",
        assistant_payload={"agent_role": "preprocessing"},
        tool=None,
        workspace_path=None,
        preferred_profile_id=None,
        project_id=source.project_id,
        active_run_id=other_run.run_id,
    )
    selected = _select(service, conversation_id, source.intent_task_id, "select-after-other-run")
    assert work_task(selected, WorkAgentRole.PREPROCESSING).run_id == other_run.run_id
    assert preprocessing_source_run(selected, None) == source.run_id
    interpreter.return_value = _interpret(
        {"correction_method": "fdr", "correction_parameters": {"q_threshold": 0.03}}
    )
    response = asyncio.run(
        service.conversations.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(content="补充 ALFF, 调整阈值。", agent_role=agent_role),
            "selected-source-followup",
        )
    )
    context = interpreter.call_args.args[0]
    assert context["work_context"]["run"]["run_id"] == source.run_id
    assert interpreter.call_args.kwargs["execution_context"].allowed_run_ids == frozenset(
        {source.run_id}
    )
    assert response.assistant_message.payload["statistical_design_proposal"]["source_run_id"] == (
        source.run_id
    )


@pytest.mark.parametrize(
    ("writer", "changed_intent"),
    [("exchange", False), ("exchange", True), ("continuation", False)],
)
def test_same_hash_draft_without_proposal_clears_old_confirmation_references(
    service, source_root, work_root, monkeypatch, writer, changed_intent
):
    conversation_id, source, _ = _seed(service, source_root, work_root, monkeypatch, complete=True)
    before = _select(service, conversation_id, source.intent_task_id, "select-same-hash-owner")
    old_task = work_task(before, WorkAgentRole.ANALYSIS)
    assert old_task.proposal_hash is not None
    assert old_task.work_task_id is not None
    payload = deepcopy(_message_for(before, source.intent_task_id).payload)
    payload.pop("statistical_design_proposal")
    if changed_intent:
        payload["analysis_draft"]["analysis_intent_task_id"] = "new-goal-same-draft-hash"
        payload["analysis_intent_envelope"]["task_id"] = "new-goal-same-draft-hash"
        payload["analysis_dependency"]["task_id"] = "new-goal-same-draft-hash"
        payload["analysis_outbox_continuation"]["intent_task_id"] = "new-goal-same-draft-hash"
    if writer == "exchange":
        after, *_ = service.repository.append_conversation_exchange(
            conversation_id,
            user_content="A draft revision with unchanged scientific fields.",
            assistant_content="The draft has no current proposal.",
            assistant_payload=payload,
            tool=None,
            workspace_path=None,
            preferred_profile_id=None,
            project_id=source.project_id,
            active_run_id=None,
        )
    else:
        after, _ = service.repository.append_assistant_continuation(
            conversation_id,
            continuation_key="same-hash-no-proposal",
            assistant_content="The draft has no current proposal.",
            assistant_payload=payload,
            project_id=source.project_id,
            work_task_id=None,
        )
    task = work_task(after, WorkAgentRole.ANALYSIS)
    assert task.draft_hash == old_task.draft_hash
    assert task.analysis_intent_task_id == payload["analysis_draft"]["analysis_intent_task_id"]
    assert task.proposal_hash is None
    assert task.proposal_message_id is None
    assert task.work_task_id is None
    assert task.plan_revision_id is None
    assert task.run_id is None


def test_readonly_result_turn_preserves_and_restores_original_proposal_message(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=True
    )
    selected = _select(service, conversation_id, source.intent_task_id, "select-before-readonly")
    original = _message_for(selected, source.intent_task_id)
    original_task = work_task(selected, WorkAgentRole.ANALYSIS)
    assert original_task.proposal_message_id == original.message_id
    assert original_task.plan_revision_id is None
    interpreter.return_value = (
        json.dumps({"answer": "Show the registered results.", "action": "get_statistical_results"}),
        {"finish_reason": "stop"},
        None,
    )
    response = asyncio.run(
        service.conversations.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(content="查看统计结果"),
            "selected-readonly-results",
        )
    )
    payload = response.assistant_message.payload
    assert payload["analysis_draft_unchanged"] is True
    assert payload["analysis_draft"] == original.payload["analysis_draft"]
    assert "statistical_design_proposal" not in payload
    after = service.conversations.get_conversation(conversation_id)
    assert work_task(after, WorkAgentRole.ANALYSIS) == original_task
    restored = _select(service, conversation_id, source.intent_task_id, "select-after-readonly")
    assert work_task(restored, WorkAgentRole.ANALYSIS) == original_task
    assert work_task(restored, WorkAgentRole.ANALYSIS).proposal_message_id == original.message_id
    assert len(service.runs.list_runs(project_id=source.project_id)) == 1


def test_continue_ignores_other_goal_pending_preprocessing_handoff(
    service, source_root, work_root, monkeypatch
):
    conversation_id, first, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=True
    )
    _select(service, conversation_id, first.intent_task_id, "select-before-background-handoff")
    second = _second_goal(service, conversation_id, first, interpreter, monkeypatch)
    conversation = service.conversations.get_conversation(conversation_id)
    second_message = _message_for(conversation, second.intent_task_id)
    service.repository.append_assistant_continuation(
        conversation_id,
        continuation_key="other-goal-pending-preprocessing-handoff",
        assistant_content="The other goal needs additional preprocessing outputs.",
        assistant_payload={
            "agent_role": "analysis",
            "analysis_intent_envelope": deepcopy(
                second_message.payload["analysis_intent_envelope"]
            ),
            "agent_handoff": {
                "from_agent": "analysis",
                "to_agent": "preprocessing",
                "status": "pending",
                "data_references": {
                    "project_id": first.project_id,
                    "source_run_id": first.run_id,
                },
            },
        },
        project_id=first.project_id,
        work_task_id=None,
        update_work_state=False,
    )
    interpreter.return_value = _interpret(
        {"correction_method": "fdr", "correction_parameters": {"q_threshold": 0.03}}
    )
    response = asyncio.run(
        service.conversations.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(content="继续"),
            "continue-selected-goal",
        )
    )
    payload = response.assistant_message.payload
    assert payload["agent_role"] == "analysis"
    assert payload["analysis_draft"]["analysis_intent_task_id"] == first.intent_task_id
    assert interpreter.call_args.args[0]["work_context"]["agent_role"] == "analysis"
    after = service.conversations.get_conversation(conversation_id)
    assert after.foreground_agent_role is WorkAgentRole.ANALYSIS
    assert work_task(after, WorkAgentRole.ANALYSIS).analysis_intent_task_id == first.intent_task_id


def test_delayed_freeze_from_another_intent_with_identical_proposal_hash_is_rejected(
    service, source_root, work_root, monkeypatch
):
    conversation_id, first, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=True
    )
    second = _second_goal(
        service, conversation_id, first, interpreter, monkeypatch, q_threshold=0.05
    )
    selected_first = _select(service, conversation_id, first.intent_task_id, "select-late-freeze-A")
    delayed_request = _design_request(selected_first, first.intent_task_id)
    selected_second = _select(
        service, conversation_id, second.intent_task_id, "select-late-freeze-B"
    )
    first_task = work_task(selected_first, WorkAgentRole.ANALYSIS)
    second_task = work_task(selected_second, WorkAgentRole.ANALYSIS)
    assert first_task.proposal_hash == second_task.proposal_hash
    assert first_task.proposal_message_id != second_task.proposal_message_id
    assert first_task.analysis_intent_task_id != second_task.analysis_intent_task_id
    plan_count = _database_table_counts(service)["plan_revisions"]
    with pytest.raises(ConflictError) as failure:
        service.statistics.create_statistical_design(delayed_request, "late-freeze-A-after-B")
    assert failure.value.code == "statistics_work_proposal_stale"
    assert service.conversations.get_conversation(conversation_id) == selected_second
    assert _database_table_counts(service)["plan_revisions"] == plan_count
    assert len(service.runs.list_runs(project_id=first.project_id)) == 1
