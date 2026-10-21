from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from neuroagent.application.automation.composite_goal import (
    CompositeGoalIdentity,
    CompositeGoalStatus,
    project_composite_goal,
)
from neuroagent.application.automation.models import (
    DEFAULT_TASK_BUDGET,
    AuthorityReferences,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WaitingCondition,
    WaitingKind,
    WorkGoal,
    WorkTask,
)
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationMode,
    ConversationRole,
    ConversationTurnCreate,
)
from neuroagent.application.contracts.projects import ProjectCreate
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.hashing import content_hash
from neuroagent.application.work_agents import analysis_dependency_for_request
from neuroagent.infrastructure.persistence.automation_repository import SqliteWorkTaskRepository
from neuroagent.infrastructure.persistence.models import PlanRevisionRow
from neuroagent.multi_agent.registry import WorkAgentRole
from tests.backend.conftest import make_project
from tests.backend.test_work_dialogue import configure, generated

_REQUEST = "先预处理这批静息态 fMRI 数据，然后对 ALFF 做两组统计比较。"  # noqa: RUF001


def _goal(service, project_id: str, conversation_id: str, key: str) -> CompositeGoalIdentity:
    explicit = analysis_dependency_for_request(
        _REQUEST,
        project_id,
        conversation_id,
        content_hash(key),
    )
    assert explicit is not None
    return CompositeGoalIdentity(
        intent_task_id=str(explicit["task_id"]),
        conversation_id=conversation_id,
        project_id=project_id,
        analysis_request_hash=content_hash(_REQUEST.strip()),
        preprocessing_task_id=None,
    )


@pytest.mark.asyncio
async def test_preprocessing_goal_and_budget_are_durable_before_gateway(
    service, source_root: Path, work_root: Path, monkeypatch
) -> None:
    project = make_project(service, source_root, work_root, key="composite-admission-project")
    conversation = service.conversations.create_conversation(
        ConversationCreate(
            mode=ConversationMode.WORK,
            project_id=project.project_id,
            workspace_path=str(source_root),
        ),
        "composite-admission-conversation",
    )
    key = "composite-admission-turn"
    goal = _goal(service, project.project_id, conversation.conversation_id, key)
    model = configure(
        service, monkeypatch, [generated("conversation", answer="我已记录这个目标。")]
    )

    async def inspect_gateway(*_args, **_kwargs):
        current = service.automation.find_composite_goal(goal.intent_task_id)
        assert current == goal
        budget = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
        assert budget.max_steps == DEFAULT_TASK_BUDGET.max_steps
        assert budget.max_model_calls == DEFAULT_TASK_BUDGET.max_model_calls
        assert budget.max_tokens == DEFAULT_TASK_BUDGET.max_tokens
        assert budget.max_elapsed_seconds == DEFAULT_TASK_BUDGET.max_elapsed_seconds
        assert budget.used_model_calls == 1
        with service.database.session_factory() as session:
            reservations = session.execute(
                text(
                    "SELECT COUNT(*) FROM composite_goal_model_call_reservations "
                    "WHERE intent_task_id=:intent_id"
                ),
                {"intent_id": goal.intent_task_id},
            ).scalar_one()
        assert reservations == 1
        stored = service.conversations.get_conversation(conversation.conversation_id)
        admitted = [
            message
            for message in stored.messages
            if message.role is ConversationRole.USER and message.content == _REQUEST
        ]
        assert len(admitted) == 1
        assert admitted[0].payload["composite_goal_admission"]["intent_task_id"] == (
            goal.intent_task_id
        )
        return generated("conversation", answer="我已记录这个目标。")

    model.side_effect = inspect_gateway
    result = await service.conversations.send_conversation_turn(
        conversation.conversation_id,
        ConversationTurnCreate(
            content=_REQUEST,
            project_id=project.project_id,
            preferred_profile_id="dialogue-model",
            agent_role=WorkAgentRole.ANALYSIS,
        ),
        key,
    )

    model.assert_awaited_once()
    assert result.user_message.payload["composite_goal_admission"]["intent_task_id"] == (
        goal.intent_task_id
    )
    assert service.automation.find_composite_goal(goal.intent_task_id).preprocessing_task_id is None
    assert service.automation.get_composite_goal_progress(
        conversation.conversation_id, goal.intent_task_id
    ).status is CompositeGoalStatus.WAITING_PREPROCESSING


@pytest.mark.asyncio
async def test_admission_failure_replay_reuses_message_root_and_budget(
    service, source_root: Path, work_root: Path, monkeypatch
) -> None:
    from neuroagent.application.conversation_work import WorkActionResult
    from neuroagent.application.work_dialogue import WorkDialogue

    project = make_project(service, source_root, work_root, key="composite-replay-project")
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode=ConversationMode.WORK, project_id=project.project_id),
        "composite-replay-conversation",
    )
    key = "composite-replay-turn"
    goal = _goal(service, project.project_id, conversation.conversation_id, key)
    attempts = 0
    admitted_message_ids: list[str] = []

    async def fail_once(_self, _conversation, _request, _packet, **kwargs):
        nonlocal attempts
        attempts += 1
        assert kwargs["persistent_model_budget"] is not None
        current = service.automation.find_composite_goal(goal.intent_task_id)
        assert current == goal
        assert (
            service.automation.get_composite_goal_model_budget(goal.intent_task_id).used_steps
            == 0
        )
        stored = service.conversations.get_conversation(conversation.conversation_id)
        admitted = [
            message
            for message in stored.messages
            if message.role is ConversationRole.USER and message.content == _REQUEST
        ]
        assert len(admitted) == 1
        admitted_message_ids.append(admitted[0].message_id)
        if attempts == 1:
            raise RuntimeError("simulated interruption after admission")
        return WorkActionResult(
            "目标已保存，等待预处理方案。",  # noqa: RUF001
            payload={"agent_role": WorkAgentRole.PREPROCESSING.value},
            project_id=project.project_id,
        )

    monkeypatch.setattr(WorkDialogue, "respond", fail_once)
    request = ConversationTurnCreate(content=_REQUEST, project_id=project.project_id)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await service.conversations.send_conversation_turn(
            conversation.conversation_id, request, key
        )

    assert service.automation.find_composite_goal(goal.intent_task_id) == goal
    assert (
        service.automation.get_composite_goal_model_budget(goal.intent_task_id).used_model_calls
        == 0
    )
    recovered = await service.conversations.send_conversation_turn(
        conversation.conversation_id, request, key
    )
    replay = await service.conversations.send_conversation_turn(
        conversation.conversation_id, request, key
    )
    assert recovered == replay
    assert attempts == 2
    assert admitted_message_ids[0] == admitted_message_ids[1] == recovered.user_message.message_id
    stored = service.conversations.get_conversation(conversation.conversation_id)
    assert sum(
        message.role is ConversationRole.USER and message.content == _REQUEST
        for message in stored.messages
    ) == 1
    assert service.automation.find_composite_goal(goal.intent_task_id) == goal


def test_unbound_root_projects_as_waiting_and_identity_is_explicit() -> None:
    goal = CompositeGoalIdentity(
        intent_task_id="early-root",
        conversation_id="conversation",
        project_id="project",
        analysis_request_hash="a" * 64,
        preprocessing_task_id=None,
    )
    from neuroagent.application.automation.composite_goal import CompositeGoalSnapshot

    progress = project_composite_goal(CompositeGoalSnapshot(goal=goal))
    assert progress.status is CompositeGoalStatus.WAITING_PREPROCESSING
    assert progress.reason_code == "preprocessing_not_started"


@pytest.mark.asyncio
async def test_admission_binds_exact_root_to_preprocessing_plan_and_task(
    service, source_root: Path, work_root: Path, monkeypatch
) -> None:
    from uuid import uuid4

    from neuroagent.application.conversation_work import WorkActionResult
    from neuroagent.infrastructure.persistence.models import PlanRevisionRow

    project = make_project(service, source_root, work_root, key="composite-plan-binding-project")
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode=ConversationMode.WORK, project_id=project.project_id),
        "composite-plan-binding-conversation",
    )
    key = "composite-plan-binding-turn"
    goal = _goal(service, project.project_id, conversation.conversation_id, key)

    async def reply(_self, _conversation, _request, _packet, **_kwargs):
        return WorkActionResult(
            "已生成预处理方案草稿。",
            payload={"agent_role": WorkAgentRole.PREPROCESSING.value},
            project_id=project.project_id,
        )

    from neuroagent.application.work_dialogue import WorkDialogue

    monkeypatch.setattr(WorkDialogue, "respond", reply)
    request = ConversationTurnCreate(
        content=_REQUEST,
        project_id=project.project_id,
        # Explicit analysis routing must not bypass compound-goal admission.
        agent_role=WorkAgentRole.ANALYSIS,
    )
    turn = await service.conversations.send_conversation_turn(
        conversation.conversation_id, request, key
    )
    admitted_message = turn.user_message
    assert admitted_message.payload["composite_goal_admission"] == {
        "intent_task_id": goal.intent_task_id,
        "conversation_id": conversation.conversation_id,
        "project_id": project.project_id,
        "analysis_request_hash": goal.analysis_request_hash,
    }

    plan_id = str(uuid4())
    plan_hash = "d" * 64
    manifest_hash = "c" * 64
    plan_json = {
        "kind": "skill_plan",
        "dataset_manifest_id": "admission-binding-manifest",
        "skill_plan": {"preprocessing_parameters_hash": "e" * 64},
    }
    with service.database.session_factory.begin() as session:
        session.add(
            PlanRevisionRow(
                plan_revision_id=plan_id,
                project_id=project.project_id,
                revision=1,
                state="awaiting_approval",
                plan_hash=plan_hash,
                manifest_hash=manifest_hash,
                environment_hash="f" * 64,
                plan_json=json.dumps(plan_json),
            )
        )
    monkeypatch.setattr(service.automation, "_derive_preprocessing_expectations", lambda *_: ())

    task_id = service.conversations._commit_preprocessing_work_task_binding(
        conversation=service.conversations.get_conversation(conversation.conversation_id),
        user_content=_REQUEST,
        payload={
            "plan_revision": {
                "plan_revision_id": plan_id,
                "project_id": project.project_id,
                "plan": plan_json,
            }
        },
        project_id=project.project_id,
        admitted_goal=goal,
        admitted_user_message_id=admitted_message.message_id,
    )

    assert task_id == service.automation.preprocessing_task_id(
        conversation.conversation_id, plan_id
    )
    task = service.automation.get_task(task_id)
    assert task.authority_refs.analysis_intent_task_id == goal.intent_task_id
    assert task.authority_refs.analysis_request_hash == goal.analysis_request_hash
    assert task.authority_refs.plan_revision_id == plan_id
    assert service.automation.get_composite_goal(
        conversation.conversation_id, goal.intent_task_id
    ).preprocessing_task_id == task_id
    assert (
        service.conversations.get_conversation(conversation.conversation_id).messages[1].message_id
        == admitted_message.message_id
    )


def test_admission_rejects_project_hash_and_second_task_mismatches(
    service, source_root: Path, work_root: Path
) -> None:
    project = make_project(service, source_root, work_root, key="composite-mismatch-project")
    other_project = service.projects.create_project(
        ProjectCreate(
            name="Other project",
            source_roots=[str(source_root)],
            work_root=str(work_root / "other"),
        ),
        "composite-mismatch-other-project",
    )
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode=ConversationMode.WORK, project_id=project.project_id),
        "composite-mismatch-conversation",
    )
    key = "composite-mismatch-turn"
    goal = _goal(service, project.project_id, conversation.conversation_id, key)
    service.automation.ensure_preprocessing_composite_goal_intent(goal)

    with pytest.raises(ConflictError):
        service.automation.ensure_preprocessing_composite_goal_intent(
            goal.model_copy(update={"project_id": other_project.project_id})
        )
    with pytest.raises(ConflictError):
        service.automation.ensure_preprocessing_composite_goal_intent(
            goal.model_copy(update={"analysis_request_hash": "b" * 64})
        )

    manifest_hash = "c" * 64
    plan_ids = ("composite-binding-plan", "composite-binding-plan-wrong")
    with service.database.session_factory.begin() as session:
        for index, plan_id in enumerate(plan_ids, start=1):
            session.add(
                PlanRevisionRow(
                    plan_revision_id=plan_id,
                    project_id=project.project_id,
                    revision=index,
                    state="awaiting_approval",
                    plan_hash="d" * 64,
                    manifest_hash=manifest_hash,
                    environment_hash="e" * 64,
                    plan_json=json.dumps(
                        {
                            "kind": "skill_plan",
                            "skill_plan": {"preprocessing_parameters_hash": "f" * 64},
                        }
                    ),
                )
            )

    with pytest.raises(ConflictError, match="复合目标"):
        service.conversations._commit_preprocessing_work_task_binding(
            conversation=service.conversations.get_conversation(conversation.conversation_id),
            user_content=_REQUEST,
            payload={
                "plan_revision": {
                    "plan_revision_id": plan_ids[0],
                    "project_id": project.project_id,
                    "plan": {
                        "kind": "skill_plan",
                        "skill_plan": {"preprocessing_parameters_hash": "f" * 64},
                    },
                },
                "work_draft": {
                    "analysis_intent_task_id": "missing-composite-root",
                    "analysis_request_hash": content_hash(_REQUEST.strip()),
                },
            },
            project_id=project.project_id,
        )
    with pytest.raises(NotFoundError):
        service.automation.get_task(
            service.automation.preprocessing_task_id(conversation.conversation_id, plan_ids[0])
        )

    def task(task_id: str, plan_id: str) -> WorkTask:
        refs = AuthorityReferences(
            conversation_id=conversation.conversation_id,
            analysis_intent_task_id=goal.intent_task_id,
            analysis_request_hash=goal.analysis_request_hash,
            plan_revision_id=plan_id,
            input_revision=manifest_hash,
        )
        return WorkTask(
            task_id=task_id,
            goal=WorkGoal(description="composite task", completion_conditions=("verified",)),
            status=TaskStatus.WAITING_USER,
            waiting_condition=WaitingCondition(
                kind=WaitingKind.USER_DECISION,
                reference=f"plan:{plan_id}",
                reason="review required",
            ),
            authority_refs=refs,
            steps=(
                TaskStep(
                    step_id=f"preprocessing:{plan_id}",
                    sequence=0,
                    kind="preprocessing_run",
                    status=TaskStepStatus.READY,
                    input_version="f" * 64,
                    authority_refs=refs,
                ),
            ),
            budget=DEFAULT_TASK_BUDGET,
        )

    first = service.automation.create_task(task("composite-binding-task", plan_ids[0]))
    assert service.automation.get_composite_goal(
        conversation.conversation_id, goal.intent_task_id
    ).preprocessing_task_id == first.task_id
    with pytest.raises(ConflictError):
        service.automation.create_task(task("composite-binding-task-wrong", plan_ids[1]))
    assert service.automation.get_composite_goal(
        conversation.conversation_id, goal.intent_task_id
    ).preprocessing_task_id == first.task_id
    with pytest.raises(NotFoundError):
        service.automation.get_task("composite-binding-task-wrong")


def test_admitted_raw_request_is_the_analysis_outbox_source(
    service, source_root: Path, work_root: Path, monkeypatch
) -> None:
    from neuroagent.infrastructure.persistence.models import WorkflowRunRow

    project = make_project(service, source_root, work_root, key="admitted-outbox-project")
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode=ConversationMode.WORK, project_id=project.project_id),
        "admitted-outbox-conversation",
    )
    goal = _goal(service, project.project_id, conversation.conversation_id, "admitted-outbox")
    message_id = "admitted-outbox-message"
    with service.idempotency_store.atomic():
        service.repository.admit_preprocessing_goal_intent_message(
            conversation.conversation_id,
            message_id=message_id,
            user_content=_REQUEST,
            intent_task_id=goal.intent_task_id,
            project_id=project.project_id,
            analysis_request_hash=goal.analysis_request_hash,
        )
        service.automation.ensure_preprocessing_composite_goal_intent(goal)

    plan_id = "admitted-outbox-plan"
    plan_hash = "c" * 64
    manifest_hash = "b" * 64
    run_id = "admitted-outbox-run"
    with service.database.session_factory.begin() as session:
        session.add(
            PlanRevisionRow(
                plan_revision_id=plan_id,
                project_id=project.project_id,
                revision=1,
                state="awaiting_approval",
                plan_hash=plan_hash,
                manifest_hash=manifest_hash,
                environment_hash="e" * 64,
                plan_json=json.dumps(
                    {
                        "kind": "skill_plan",
                        "skill_plan": {"preprocessing_parameters_hash": "d" * 64},
                    }
                ),
            )
        )
        session.add(
            WorkflowRunRow(
                run_id=run_id,
                project_id=project.project_id,
                plan_revision_id=plan_id,
                state="succeeded",
                version=1,
            )
        )
    refs = AuthorityReferences(
        conversation_id=conversation.conversation_id,
        analysis_intent_task_id=goal.intent_task_id,
        analysis_request_hash=goal.analysis_request_hash,
        plan_revision_id=plan_id,
        input_revision=manifest_hash,
        workflow_run_id=run_id,
    )
    task = WorkTask(
        task_id="admitted-outbox-preprocessing-task",
        goal=WorkGoal(description="预处理并核验用户请求", completion_conditions=("verified",)),
        status=TaskStatus.WAITING_USER,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.USER_DECISION,
            reference=f"plan:{plan_id}",
            reason="review required",
        ),
        authority_refs=refs,
        steps=(
            TaskStep(
                step_id="admitted-outbox-preprocessing-step",
                sequence=0,
                kind="preprocessing_run",
                status=TaskStepStatus.READY,
                input_version=plan_hash,
                authority_refs=refs,
            ),
        ),
        budget=DEFAULT_TASK_BUDGET,
    )
    service.automation.create_task(task)
    outbox = service.automation.ensure_analysis_proposal_outbox(
        intent={
            "task_kind": "preprocess_then_analyze",
            "task_id": goal.intent_task_id,
            "conversation_id": conversation.conversation_id,
            "project_id": project.project_id,
            "analysis_request": _REQUEST,
        },
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        preprocessing_task_id=task.task_id,
    )
    assert outbox is not None

    assert service.automation._outbox_intent_matches(outbox)
    with service.database.session_factory() as session:
        assert SqliteWorkTaskRepository._analysis_outbox_source_matches(session, outbox)

    class AdmittedRequestVerified(Exception):
        pass

    monkeypatch.setattr(
        service.automation._repository,
        "get_task",
        lambda _task_id: (_ for _ in ()).throw(AdmittedRequestVerified()),
    )
    with pytest.raises(AdmittedRequestVerified):
        service.automation.verify_analysis_outbox_source(outbox)

    with service.database.session_factory.begin() as session:
        session.execute(
            text("UPDATE conversation_messages SET content=:content WHERE message_id=:id"),
            {"content": "替换后的请求文本", "id": message_id},
        )
    assert not service.automation._outbox_intent_matches(outbox)
    with service.database.session_factory() as session:
        assert not SqliteWorkTaskRepository._analysis_outbox_source_matches(session, outbox)
    with pytest.raises(ConflictError) as failure:
        service.automation.verify_analysis_outbox_source(outbox)
    assert failure.value.code == "analysis_outbox_intent_mismatch"
