"""Frozen goal restoration validates history without creating scientific work."""

from __future__ import annotations

from copy import deepcopy

import pytest

from neuroagent.application.automation.models import TaskStatus, TaskStepStatus
from neuroagent.application.automation.result_interpretation import (
    ResultInterpretationStatus,
    StatisticalResultInterpretationOutbox,
)
from neuroagent.application.contracts.conversations import WorkAgentSelection
from neuroagent.application.contracts.statistics import (
    StatisticalDesignValidationRequest,
    StatisticsRunCreate,
)
from neuroagent.application.errors import ConflictError
from neuroagent.application.hashing import content_hash
from neuroagent.application.statistics_draft import StatisticsDraft, statistics_draft_hash
from neuroagent.application.work_tasks import work_task
from neuroagent.bootstrap import build_worker
from neuroagent.multi_agent.registry import WorkAgentRole
from tests.backend.test_composite_goal_selection import _design_request, _message_for, _select
from tests.backend.test_consumed_analysis_intent import _seed
from tests.backend.test_work_analysis_handoffs import _database_table_counts
from tests.backend.test_work_task_bindings import _approve


def _frozen(service, source_root, work_root, monkeypatch, phase="draft"):
    conversation_id, source, interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=True
    )
    conversation = _select(service, conversation_id, source.intent_task_id, "frozen-select-initial")
    frozen = service.statistics.create_statistical_design(
        _design_request(conversation, source.intent_task_id), "frozen-create-plan"
    )
    plan = frozen.plan_revision
    if phase != "draft":
        service.statistics.validate_statistical_design(
            plan.plan_revision_id,
            StatisticalDesignValidationRequest(expected_version=plan.version),
            "frozen-validate-plan",
        )
    if phase not in {"draft", "awaiting_approval"}:
        _approve(service, plan.plan_revision_id)
    run = None
    if phase in {"queued", "waiting_result_interpretation", "completed"}:
        run = service.runs.create_statistics_run(
            StatisticsRunCreate(
                project_id=source.project_id,
                statistical_design_revision_id=plan.plan_revision_id,
                expected_plan_hash=plan.plan_hash,
            ),
            "frozen-create-mock-run",
        )
    if phase in {"waiting_result_interpretation", "completed"}:
        assert build_worker(service).run_once()
        run = service.runs.get_run(run.run_id)
        goal = service.automation.get_composite_goal(conversation_id, source.intent_task_id)
        task = service.automation.get_task(goal.analysis_work_task_id)
        # This fixture exercises stored identity only. It deliberately does not
        # run the scientific result verifier or claim that Mock output is real.
        task = task.model_copy(
            update={
                "version": task.version + 1,
                "status": TaskStatus.COMPLETED,
                "waiting_condition": None,
                "steps": (task.steps[0].model_copy(update={"status": TaskStepStatus.SUCCEEDED}),),
            }
        )
        service.automation._repository.save_task(task, expected_version=task.version - 1)
        outbox = StatisticalResultInterpretationOutbox(
            task_id=task.task_id,
            conversation_id=conversation_id,
            project_id=source.project_id,
            run_id=run.run_id,
            plan_revision_id=plan.plan_revision_id,
            result_id="fixture-only-result-identity",
            result_bundle_hash="d" * 64,
            result_version=1,
            artifact_ids=("fixture-only-result-artifact",),
        )
        service.automation.ensure_result_interpretation_outbox(outbox)
        if phase == "completed":
            goal = service.automation.get_composite_goal(conversation_id, source.intent_task_id)
            claim = service.automation.claim_next_result_interpretation_outbox(
                "frozen-fixture-worker", lease_seconds=30
            )
            _, message = service.repository.append_assistant_continuation(
                conversation_id,
                continuation_key="frozen-result-identity-fixture",
                assistant_content="Fixture-only interpretation identity.",
                assistant_payload={
                    "agent_role": "analysis",
                    "analysis_intent_envelope": {
                        "task_id": source.intent_task_id,
                        "status": "completed",
                        "conversation_id": conversation_id,
                        "project_id": source.project_id,
                        "analysis_request_hash": goal.analysis_request_hash,
                    },
                    "statistical_result_interpretation": {
                        "task_id": task.task_id,
                        "status": "completed",
                        "result_id": outbox.result_id,
                        "result_version": outbox.result_version,
                    },
                },
                project_id=source.project_id,
                work_task_id=None,
                update_work_state=False,
            )
            service.automation.complete_result_interpretation_outbox(
                claim,
                status=ResultInterpretationStatus.COMPLETED,
                continuation_message_id=message.message_id,
            )
    return conversation_id, source, service.plans.get_plan(plan.plan_revision_id), run, interpreter


@pytest.mark.parametrize(
    "phase",
    [
        "draft",
        "awaiting_approval",
        "approved",
        "queued",
        "waiting_result_interpretation",
        "completed",
    ],
)
def test_frozen_goal_restores_original_identity_without_environment_or_writes(
    service, source_root, work_root, monkeypatch, phase
):
    conversation_id, source, plan, run, interpreter = _frozen(
        service, source_root, work_root, monkeypatch, phase
    )
    before = service.conversations.get_conversation(conversation_id)
    original = _message_for(before, source.intent_task_id)
    goal = service.automation.get_composite_goal(conversation_id, source.intent_task_id)
    task = service.automation.get_task(goal.analysis_work_task_id)
    counts = _database_table_counts(service)
    interpreter.reset_mock()
    monkeypatch.setattr(
        service.environment_provider, "current", lambda: pytest.fail("selection probed environment")
    )
    selected = _select(service, conversation_id, source.intent_task_id, "restore-frozen-goal")
    restored = work_task(selected, WorkAgentRole.ANALYSIS)
    assert restored.plan_revision_id == plan.plan_revision_id
    assert restored.proposal_message_id == original.message_id
    assert restored.proposal_hash == plan.plan["work_context"]["proposal_hash"]
    assert restored.draft_hash == plan.plan["work_context"]["draft_hash"]
    assert restored.work_task_id == goal.analysis_work_task_id
    assert restored.run_id == (run.run_id if run else None)
    assert selected.active_run_id == restored.run_id
    assert selected.messages == before.messages
    assert service.automation.get_task(task.task_id) == task
    assert service.plans.get_plan(plan.plan_revision_id) == plan
    assert service.conversations.resolve_selected_frozen_analysis(selected) == (restored, plan)
    interpreter.assert_not_called()
    after_counts = _database_table_counts(service)
    assert {
        key: value
        for key, value in counts.items()
        if "approval" in key or key in {"plan_revisions", "workflow_runs"}
    } == {
        key: value
        for key, value in after_counts.items()
        if "approval" in key or key in {"plan_revisions", "workflow_runs"}
    }


def test_frozen_composite_task_without_intent_identity_fails_closed(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, _, _, _ = _frozen(
        service, source_root, work_root, monkeypatch, "queued"
    )
    selected = _select(service, conversation_id, source.intent_task_id, "restore-missing-intent")
    analysis = work_task(selected, WorkAgentRole.ANALYSIS)
    assert analysis is not None and analysis.work_task_id is not None
    without_intent = analysis.model_copy(update={"analysis_intent_task_id": None})
    broken = selected.model_copy(
        update={
            "work_tasks": tuple(
                without_intent if task.agent_role is WorkAgentRole.ANALYSIS else task
                for task in selected.work_tasks
            )
        }
    )

    with pytest.raises(ConflictError) as failure:
        service.conversations.resolve_selected_frozen_analysis(broken)

    assert failure.value.code == "analysis_goal_selection_conflict"


@pytest.mark.parametrize(
    "mutation", ["message", "plan_hash", "plan_design", "step", "run", "project", "request_hash"]
)
def test_frozen_identity_conflict_does_not_change_selection(
    service, source_root, work_root, monkeypatch, mutation
):
    conversation_id, source, plan, run, _ = _frozen(
        service, source_root, work_root, monkeypatch, "queued"
    )
    before = service.conversations.get_conversation(conversation_id)
    goal = service.automation.get_composite_goal(conversation_id, source.intent_task_id)
    task = service.automation.get_task(goal.analysis_work_task_id)
    if mutation in {"message", "plan_hash", "plan_design", "project"}:
        changed = plan.model_copy(deep=True)
        if mutation == "message":
            changed.plan["work_context"]["proposal_message_id"] = "another-message"
        elif mutation == "plan_design":
            changed.plan["design"]["one_sample_baseline"] = 42
        elif mutation == "project":
            changed = changed.model_copy(update={"project_id": "another-project"})
        if mutation == "plan_hash":
            changed = changed.model_copy(update={"plan_hash": "e" * 64})
        else:
            changed = changed.model_copy(
                update={
                    "plan_hash": content_hash(
                        {
                            "plan": changed.plan,
                            "manifest_hash": changed.manifest_hash,
                            "environment_hash": changed.environment_hash,
                        }
                    )
                }
            )
        monkeypatch.setattr(service.conversations, "get_plan", lambda _: changed)
    elif mutation == "run":
        monkeypatch.setattr(
            service.conversations,
            "get_run",
            lambda _: run.model_copy(update={"plan_revision_id": "another-plan"}),
        )
    else:
        step = task.steps[0]
        step = step.model_copy(
            update={
                "authority_refs": step.authority_refs.model_copy(
                    update={"plan_revision_id": "another-plan"}
                    if mutation == "step"
                    else {"analysis_request_hash": "e" * 64}
                )
            }
        )
        monkeypatch.setattr(
            service.automation, "get_task", lambda _: task.model_copy(update={"steps": (step,)})
        )
    with pytest.raises(ConflictError) as failure:
        _select(service, conversation_id, source.intent_task_id, "restore-frozen-conflict")
    assert failure.value.code == "analysis_goal_selection_conflict"
    assert service.conversations.get_conversation(conversation_id) == before


@pytest.mark.parametrize("kind", ["changed", "incomplete", "readonly"])
def test_newer_draft_guard_keeps_frozen_message_identity(
    service, source_root, work_root, monkeypatch, kind
):
    conversation_id, source, plan, _, _ = _frozen(service, source_root, work_root, monkeypatch)
    before = service.conversations.get_conversation(conversation_id)
    original = _message_for(before, source.intent_task_id)
    payload = deepcopy(original.payload)
    if kind == "changed":
        draft = StatisticsDraft.model_validate(payload["analysis_draft"]["draft"]).model_copy(
            update={"one_sample_baseline": 42}
        )
        payload["analysis_draft"].update(
            draft=draft.model_dump(mode="json"), hash=statistics_draft_hash(draft)
        )
    elif kind == "incomplete":
        payload.pop("statistical_design_proposal")
    else:
        payload.pop("statistical_design_proposal")
        payload["analysis_draft_unchanged"] = True
    service.repository.append_assistant_continuation(
        conversation_id,
        continuation_key=f"newer-{kind}-frozen-snapshot",
        assistant_content="Fixture later snapshot.",
        assistant_payload=payload,
        project_id=source.project_id,
        work_task_id=None,
        update_work_state=False,
    )
    if kind == "readonly":
        selected = _select(
            service, conversation_id, source.intent_task_id, "restore-newer-snapshot"
        )
        assert (
            work_task(selected, WorkAgentRole.ANALYSIS).proposal_message_id == original.message_id
        )
        assert work_task(selected, WorkAgentRole.ANALYSIS).plan_revision_id == plan.plan_revision_id
    else:
        with pytest.raises(ConflictError):
            _select(service, conversation_id, source.intent_task_id, "restore-newer-snapshot")


def test_frozen_selection_replay_and_event_failure_do_not_change_later_frontend_state(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, _, _, _ = _frozen(
        service, source_root, work_root, monkeypatch, "queued"
    )
    before = service.conversations.get_conversation(conversation_id)
    original_append = service.conversations.event_store.append_event
    monkeypatch.setattr(
        service.conversations.event_store,
        "append_event",
        lambda **_: (_ for _ in ()).throw(RuntimeError("fixture event failure")),
    )
    with pytest.raises(RuntimeError, match="fixture event failure"):
        _select(service, conversation_id, source.intent_task_id, "restore-frozen-retry")
    assert service.conversations.get_conversation(conversation_id) == before
    monkeypatch.setattr(service.conversations.event_store, "append_event", original_append)
    selected = _select(
        service,
        conversation_id,
        source.intent_task_id,
        "restore-frozen-retry",
        version=before.version,
    )
    switched = service.conversations.select_work_agent(
        conversation_id,
        WorkAgentSelection(agent_role="preprocessing", expected_version=selected.version),
        "frozen-switch-role-after-selection",
    )
    assert (
        _select(
            service,
            conversation_id,
            source.intent_task_id,
            "restore-frozen-retry",
            version=before.version,
        )
        == selected
    )
    assert service.conversations.get_conversation(conversation_id) == switched
