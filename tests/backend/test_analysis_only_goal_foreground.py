from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from neuroagent.api.schemas.conversations import (
    ConversationTurnCreate as HttpConversationTurnCreate,
)
from neuroagent.api.schemas.mapping import to_application_message
from neuroagent.application.automation.models import TaskStatus
from neuroagent.application.contracts.conversations import (
    CompositeGoalSelection,
    ConversationCreate,
    ConversationTurnCreate,
)
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.hashing import content_hash
from neuroagent.multi_agent.registry import WorkAgentRole
from tests.backend.test_consumed_analysis_intent import _seed
from tests.backend.test_work_dialogue import configure, generated
from tests.backend.test_work_task_bindings import _external_analysis_target, _proposal


def _followup(goal, content: str) -> ConversationTurnCreate:
    return ConversationTurnCreate(content=content, project_id=goal.project_id)


def _goal_count(service, conversation_id: str) -> int:
    with service.database.engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM composite_goal_links "
                    "WHERE conversation_id=:conversation_id "
                    "AND goal_kind='analysis_only_external_import'"
                ),
                {"conversation_id": conversation_id},
            ).scalar_one()
        )


def test_analysis_only_followup_keeps_original_root_and_cumulative_budget(
    service, source_root, work_root, monkeypatch
):
    goal, _role_task, _run, original_payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    original_goal = service.automation.get_composite_goal(goal.conversation_id, goal.intent_task_id)
    original_budget = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    original_request = original_payload["analysis_dependency"]["analysis_request"]
    key = "analysis-followup-private-key"
    provider = AsyncMock(return_value=generated("conversation", answer="已补充分析说明。"))
    monkeypatch.setattr(service.models, "_generate_rsfmri_chat", provider)

    response = asyncio.run(
        service.conversations.send_conversation_turn(
            goal.conversation_id,
            _followup(goal, "请进一步解释这个统计选择。"),
            key,
        )
    )

    payload = response.assistant_message.payload
    assert payload["analysis_target"]["intent_task_id"] == goal.intent_task_id
    assert payload["analysis_dependency"]["analysis_request"] == original_request
    assert payload["analysis_draft"]["analysis_intent_task_id"] == goal.intent_task_id
    assert (
        service.automation.get_composite_goal(goal.conversation_id, goal.intent_task_id)
        == original_goal
    )
    assert (
        service.automation.get_composite_goal_model_budget(goal.intent_task_id).used_model_calls
        == original_budget.used_model_calls + 1
    )
    assert _goal_count(service, goal.conversation_id) == 1
    with service.database.engine.connect() as connection:
        reservations = connection.execute(
            text(
                "SELECT reservation_id, call_id FROM composite_goal_model_call_reservations "
                "WHERE intent_task_id=:intent_id"
            ),
            {"intent_id": goal.intent_task_id},
        ).all()
        raw_key_count = connection.execute(
            text("SELECT COUNT(*) FROM idempotency_records WHERE idempotency_key=:key"),
            {"key": key},
        ).scalar_one()
    assert key not in repr(reservations)
    assert raw_key_count == 0


def test_analysis_slot_change_during_turn_setup_never_persists_raw_key(
    service, source_root, work_root, monkeypatch
):
    goal, _role_task, _run, _original_payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    actual_get_conversation = service.conversations.repository.get_conversation
    initial_read = True
    observed_analysis_slots: list[str | None] = []

    def read_with_stale_empty_analysis_slot(conversation_id: str):
        nonlocal initial_read
        conversation = actual_get_conversation(conversation_id)
        if initial_read:
            initial_read = False
            work_tasks = [
                task.model_copy(update={"analysis_intent_task_id": None})
                if task.agent_role is WorkAgentRole.ANALYSIS
                else task
                for task in conversation.work_tasks
            ]
            stale = conversation.model_copy(update={"work_tasks": work_tasks})
            observed_analysis_slots.append(
                next(
                    task.analysis_intent_task_id
                    for task in stale.work_tasks
                    if task.agent_role is WorkAgentRole.ANALYSIS
                )
            )
            return stale
        observed_analysis_slots.append(
            next(
                task.analysis_intent_task_id
                for task in conversation.work_tasks
                if task.agent_role is WorkAgentRole.ANALYSIS
            )
        )
        return conversation

    monkeypatch.setattr(
        service.conversations.repository,
        "get_conversation",
        read_with_stale_empty_analysis_slot,
    )
    provider = AsyncMock(return_value=generated("conversation", answer="不应调用模型。"))
    monkeypatch.setattr(service.models, "_generate_rsfmri_chat", provider)
    raw_key = "analysis-slot-race-private-key"

    with pytest.raises(ConflictError) as changed_slot:
        asyncio.run(
            service.conversations.send_conversation_turn(
                goal.conversation_id,
                ConversationTurnCreate(
                    content="请继续解释当前分析目标。",
                    project_id=goal.project_id,
                    agent_role=WorkAgentRole.ANALYSIS,
                ),
                raw_key,
            )
        )

    assert changed_slot.value.code == "analysis_goal_selection_conflict"
    assert observed_analysis_slots == [None, goal.intent_task_id]
    provider.assert_not_awaited()
    with service.database.engine.connect() as connection:
        raw_key_count = connection.execute(
            text("SELECT COUNT(*) FROM idempotency_records WHERE idempotency_key=:key"),
            {"key": raw_key},
        ).scalar_one()
    assert raw_key_count == 0


@pytest.mark.parametrize(
    ("requested_role", "content"),
    [
        (WorkAgentRole.PREPROCESSING, "继续准备预处理方案。"),
        (None, "开始一个新的预处理任务。"),
    ],
)
def test_preprocessing_turn_does_not_consume_selected_analysis_only_goal(
    service, source_root, work_root, monkeypatch, requested_role, content
):
    goal, _role_task, _run, _original_payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    before_budget = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    provider = AsyncMock(return_value=generated("conversation", answer="继续准备预处理方案。"))
    monkeypatch.setattr(service.models, "_generate_rsfmri_chat", provider)
    response = asyncio.run(
        service.conversations.send_conversation_turn(
            goal.conversation_id,
            ConversationTurnCreate(
                content=content,
                project_id=goal.project_id,
                agent_role=requested_role,
            ),
            "explicit-preprocessing-with-analysis-slot",
        )
    )

    assert response.assistant_message.payload["agent_role"] == WorkAgentRole.PREPROCESSING.value
    provider.assert_awaited_once()
    after_budget = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    assert after_budget == before_budget
    selected = next(
        item
        for item in service.conversations.get_conversation(goal.conversation_id).work_tasks
        if item.agent_role is WorkAgentRole.ANALYSIS
    )
    assert selected.analysis_intent_task_id == goal.intent_task_id


def test_explicit_preprocessing_turn_keeps_selected_preprocess_then_analyze_goal_separate(
    service, source_root, work_root, monkeypatch
):
    conversation_id, source, _interpreter = _seed(
        service, source_root, work_root, monkeypatch, complete=True
    )
    goal = service.automation.get_composite_goal(conversation_id, source.intent_task_id)
    conversation = service.conversations.get_conversation(conversation_id)
    service.conversations.select_composite_goal(
        conversation_id,
        goal.intent_task_id,
        CompositeGoalSelection(expected_version=conversation.version),
        "select-preprocess-then-analyze-before-preprocessing-turn",
    )
    before_budget = service.automation.get_composite_goal_model_budget(goal.intent_task_id)

    response = asyncio.run(
        service.conversations.send_conversation_turn(
            conversation_id,
            ConversationTurnCreate(
                content="继续准备预处理方案。",
                project_id=goal.project_id,
                agent_role=WorkAgentRole.PREPROCESSING,
            ),
            "explicit-preprocessing-with-composite-goal",
        )
    )

    assert response.assistant_message.payload["agent_role"] == WorkAgentRole.PREPROCESSING.value
    assert service.automation.get_composite_goal_model_budget(goal.intent_task_id) == before_budget
    after = service.conversations.get_conversation(conversation_id)
    selected = next(item for item in after.work_tasks if item.agent_role is WorkAgentRole.ANALYSIS)
    assert selected.analysis_intent_task_id == goal.intent_task_id


def test_missing_analysis_only_budget_fails_closed_before_provider(
    service, source_root, work_root, monkeypatch
):
    _old_conversation_id, request, source_payload = _proposal(service, source_root, work_root)
    source_id = source_payload["statistical_design_proposal"]["design"]["external_import_id"]
    imported = service.external_imports.get_external_analysis_import(source_id)
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=request.project_id),
        "analysis-only-missing-ledger-conversation",
    )
    original_request = "请对这个外部来源开展独立统计分析。"
    goal = service.automation.ensure_analysis_only_external_goal(
        conversation_id=conversation.conversation_id,
        project_id=request.project_id,
        analysis_request=original_request,
        imported=imported,
        idempotency_key_hash=content_hash("missing-ledger-root"),
    )
    service.repository.append_assistant_continuation(
        conversation.conversation_id,
        continuation_key="analysis-only-missing-ledger-intent",
        assistant_content="已登记独立分析请求。",
        assistant_payload={
            "agent_role": WorkAgentRole.ANALYSIS.value,
            "analysis_dependency": {
                "task_kind": goal.goal_kind,
                "task_id": goal.intent_task_id,
                "conversation_id": goal.conversation_id,
                "project_id": goal.project_id,
                "analysis_request": original_request,
                "data_references": {
                    "external_import_id": goal.source_external_import_id,
                    "external_import_version": goal.source_import_version,
                    "source_tree_hash": goal.source_tree_hash,
                    "inclusion_hash": goal.source_inclusion_hash,
                    "input_manifest_hash": goal.source_manifest_hash,
                },
            },
        },
        project_id=goal.project_id,
        work_task_id=None,
        update_work_state=False,
    )
    latest = service.conversations.get_conversation(goal.conversation_id)
    service.conversations.select_composite_goal(
        goal.conversation_id,
        goal.intent_task_id,
        CompositeGoalSelection(expected_version=latest.version),
        "analysis-only-missing-ledger-select",
    )
    before = service.conversations.get_conversation(goal.conversation_id)
    with service.database.engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM composite_goal_model_call_reservations WHERE intent_task_id=:intent_id"
            ),
            {"intent_id": goal.intent_task_id},
        )
        connection.execute(
            text("DELETE FROM composite_goal_model_budgets WHERE intent_task_id=:intent_id"),
            {"intent_id": goal.intent_task_id},
        )
    provider = configure(service, monkeypatch, [generated("conversation", answer="不应调用模型。")])
    service.work_dialogue.tool_runner = None

    result = asyncio.run(
        service.conversations.send_conversation_turn(
            goal.conversation_id,
            _followup(goal, "请继续说明。"),
            "missing-analysis-budget",
        )
    )

    provider.assert_not_awaited()
    assert result.assistant_message.payload["model_error"]["code"]
    with pytest.raises(NotFoundError):
        service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    after = service.conversations.get_conversation(goal.conversation_id)
    assert after.version == before.version + 1


def test_analysis_only_source_version_mismatch_is_rejected_before_model(
    service, source_root, work_root, monkeypatch
):
    goal, _role_task, _run, _payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    before = service.conversations.get_conversation(goal.conversation_id)
    with service.database.engine.begin() as connection:
        connection.execute(
            text("UPDATE external_analysis_imports SET version=version+1 WHERE import_id=:id"),
            {"id": goal.source_external_import_id},
        )
    provider = AsyncMock(side_effect=AssertionError("stale source must block model calls"))
    monkeypatch.setattr(service.models, "_generate_rsfmri_chat", provider)

    with pytest.raises(ConflictError) as failure:
        asyncio.run(
            service.conversations.send_conversation_turn(
                goal.conversation_id,
                _followup(goal, "请继续说明。"),
                "stale-analysis-source",
            )
        )

    assert failure.value.code == "analysis_goal_external_source_changed"
    provider.assert_not_awaited()
    assert service.conversations.get_conversation(goal.conversation_id) == before


def test_analysis_only_selection_rejects_changed_source_revision(
    service, source_root, work_root, monkeypatch
):
    goal, _role_task, _run, _payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    before = service.conversations.get_conversation(goal.conversation_id)
    with service.database.engine.begin() as connection:
        connection.execute(
            text("UPDATE external_analysis_imports SET version=version+1 WHERE import_id=:id"),
            {"id": goal.source_external_import_id},
        )

    with pytest.raises(ConflictError) as failure:
        service.conversations.select_composite_goal(
            goal.conversation_id,
            goal.intent_task_id,
            CompositeGoalSelection(expected_version=before.version),
            "select-stale-analysis-source",
        )

    assert failure.value.code == "analysis_goal_external_source_changed"
    assert service.conversations.get_conversation(goal.conversation_id) == before


@pytest.mark.parametrize("race", ["source", "analysis_slot"])
def test_analysis_only_finalize_rejects_source_or_active_slot_race(
    service, source_root, work_root, monkeypatch, race
):
    goal, _role_task, _run, _payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    before = service.conversations.get_conversation(goal.conversation_id)
    imported = service.external_imports.get_external_analysis_import(goal.source_external_import_id)
    other = None
    if race == "analysis_slot":
        other_request = "另一个明确独立的外部指标分析请求。"
        other = service.automation.ensure_analysis_only_external_goal(
            conversation_id=goal.conversation_id,
            project_id=goal.project_id,
            analysis_request=other_request,
            imported=imported,
            idempotency_key_hash=content_hash("slot-race-new-root"),
        )
        service.repository.append_assistant_continuation(
            goal.conversation_id,
            continuation_key="seed-analysis-slot-race-root",
            assistant_content="另一个外部指标目标已登记。",
            assistant_payload={
                "agent_role": WorkAgentRole.ANALYSIS.value,
                "analysis_dependency": {
                    "task_kind": other.goal_kind,
                    "task_id": other.intent_task_id,
                    "conversation_id": other.conversation_id,
                    "project_id": other.project_id,
                    "analysis_request": other_request,
                    "data_references": {
                        "external_import_id": other.source_external_import_id,
                        "external_import_version": other.source_import_version,
                        "source_tree_hash": other.source_tree_hash,
                        "inclusion_hash": other.source_inclusion_hash,
                        "input_manifest_hash": other.source_manifest_hash,
                    },
                },
            },
            project_id=other.project_id,
            work_task_id=None,
            update_work_state=False,
        )
    before = service.conversations.get_conversation(goal.conversation_id)

    async def race_during_provider(**_kwargs):
        if race == "source":
            with service.database.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE external_analysis_imports SET version=version+1 WHERE import_id=:id"
                    ),
                    {"id": goal.source_external_import_id},
                )
        else:
            assert other is not None
            with service.database.engine.begin() as connection:
                raw_tasks = connection.execute(
                    text("SELECT work_tasks_json FROM conversations WHERE conversation_id=:id"),
                    {"id": goal.conversation_id},
                ).scalar_one()
                work_tasks = json.loads(raw_tasks)
                work_tasks["analysis"]["analysis_intent_task_id"] = other.intent_task_id
                connection.execute(
                    text(
                        "UPDATE conversations SET work_tasks_json=:work_tasks "
                        "WHERE conversation_id=:id"
                    ),
                    {
                        "id": goal.conversation_id,
                        "work_tasks": json.dumps(work_tasks),
                    },
                )
        with service.database.engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM idempotency_records WHERE idempotency_key=:key"),
                    {"key": f"finalize-race-{race}"},
                ).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM idempotency_records WHERE idempotency_key=:key"),
                    {"key": content_hash(f"finalize-race-{race}")},
                ).scalar_one()
                == 1
            )
        return generated("conversation", answer="迟到的分析回复。")

    provider = AsyncMock(side_effect=race_during_provider)
    monkeypatch.setattr(service.models, "_generate_rsfmri_chat", provider)

    with pytest.raises(ConflictError):
        asyncio.run(
            service.conversations.send_conversation_turn(
                goal.conversation_id,
                _followup(goal, "请继续说明。"),
                f"finalize-race-{race}",
            )
        )

    provider.assert_awaited_once()
    after = service.conversations.get_conversation(goal.conversation_id)
    assert after.version == before.version
    assert after.messages == before.messages
    if race == "analysis_slot":
        selected = next(
            item for item in after.work_tasks if item.agent_role is WorkAgentRole.ANALYSIS
        )
        assert other is not None
        assert selected.analysis_intent_task_id == other.intent_task_id
    else:
        assert after.messages == before.messages


def test_explicit_new_analysis_goals_replay_by_key_and_fork_for_different_keys(
    service, source_root, work_root, monkeypatch
):
    goal, _role_task, _run, _payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    source_id = goal.source_external_import_id
    request = ConversationTurnCreate(
        content="请重新分析所选外部指标。",
        project_id=goal.project_id,
        external_analysis_import_id=source_id,
        start_new_analysis_goal=True,
    )
    provider = AsyncMock(return_value=generated("conversation", answer="新目标已建立。"))
    monkeypatch.setattr(service.models, "_generate_rsfmri_chat", provider)
    first_key = "new-analysis-private-key-one"

    first = asyncio.run(
        service.conversations.send_conversation_turn(goal.conversation_id, request, first_key)
    )
    replay = asyncio.run(
        service.conversations.send_conversation_turn(goal.conversation_id, request, first_key)
    )
    second = asyncio.run(
        service.conversations.send_conversation_turn(
            goal.conversation_id, request, "new-analysis-private-key-two"
        )
    )

    first_id = first.assistant_message.payload["analysis_target"]["intent_task_id"]
    second_id = second.assistant_message.payload["analysis_target"]["intent_task_id"]
    assert first_id != goal.intent_task_id
    assert second_id != first_id
    assert replay == first
    assert _goal_count(service, goal.conversation_id) == 3
    assert provider.await_count == 2
    for key in (first_key, "new-analysis-private-key-two"):
        assert key not in repr(service.automation.get_composite_goal_model_budget(first_id))
    with service.database.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT reservation_id, call_id FROM composite_goal_model_call_reservations "
                "WHERE intent_task_id IN (:first_id, :second_id)"
            ),
            {"first_id": first_id, "second_id": second_id},
        ).all()
        raw_key_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM idempotency_records "
                "WHERE idempotency_key IN (:first_key, :second_key)"
            ),
            {
                "first_key": first_key,
                "second_key": "new-analysis-private-key-two",
            },
        ).scalar_one()
    assert first_key not in repr(rows)
    assert "new-analysis-private-key-two" not in repr(rows)
    assert raw_key_count == 0


def test_work_turn_rekeys_legacy_raw_idempotency_record_before_replay(
    service, source_root, work_root, monkeypatch
):
    goal, _role_task, _run, _payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    http_request = HttpConversationTurnCreate(
        content="请继续解释当前统计目标。", project_id=goal.project_id
    )
    request = to_application_message(http_request, ConversationTurnCreate)
    assert not {
        "analysis_intent_task_id",
        "start_new_analysis_goal",
    }.intersection(request.model_fields_set)
    raw_key = "legacy-work-turn-idempotency-key"
    key_digest = content_hash(raw_key)
    provider = AsyncMock(return_value=generated("conversation", answer="已完成的旧版请求。"))
    monkeypatch.setattr(service.models, "_generate_rsfmri_chat", provider)

    first = asyncio.run(
        service.conversations.send_conversation_turn(goal.conversation_id, request, raw_key)
    )
    legacy_request_hash = content_hash(
        request.model_dump(
            mode="json",
            exclude={"analysis_intent_task_id", "start_new_analysis_goal"},
        )
    )
    with service.database.engine.begin() as connection:
        changed = connection.execute(
            text(
                "UPDATE idempotency_records "
                "SET idempotency_key=:raw_key, request_hash=:legacy_request_hash "
                "WHERE scope=:scope AND idempotency_key=:key_digest"
            ),
            {
                "raw_key": raw_key,
                "legacy_request_hash": legacy_request_hash,
                "scope": f"conversations:{goal.conversation_id}:turns",
                "key_digest": key_digest,
            },
        ).rowcount
    assert changed == 1

    replay_request = to_application_message(http_request, ConversationTurnCreate)
    replay = asyncio.run(
        service.conversations.send_conversation_turn(goal.conversation_id, replay_request, raw_key)
    )

    assert replay == first
    provider.assert_awaited_once()
    with service.database.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM idempotency_records WHERE idempotency_key=:key"),
                {"key": raw_key},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM idempotency_records WHERE idempotency_key=:key"),
                {"key": key_digest},
            ).scalar_one()
            == 1
        )


def test_legacy_work_key_rejects_changed_request_without_losing_original_replay(
    service, source_root, work_root, monkeypatch
):
    goal, _role_task, _run, _payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    original_http_request = HttpConversationTurnCreate(
        content="请继续解释当前统计目标。", project_id=goal.project_id
    )
    original_request = to_application_message(original_http_request, ConversationTurnCreate)
    raw_key = "legacy-work-key-changed-request"
    key_digest = content_hash(raw_key)
    provider = AsyncMock(return_value=generated("conversation", answer="已完成的旧版请求。"))
    monkeypatch.setattr(service.models, "_generate_rsfmri_chat", provider)

    first = asyncio.run(
        service.conversations.send_conversation_turn(
            goal.conversation_id, original_request, raw_key
        )
    )
    legacy_request_hash = content_hash(
        original_request.model_dump(
            mode="json",
            exclude={"analysis_intent_task_id", "start_new_analysis_goal"},
        )
    )
    with service.database.engine.begin() as connection:
        changed = connection.execute(
            text(
                "UPDATE idempotency_records "
                "SET idempotency_key=:raw_key, request_hash=:legacy_request_hash "
                "WHERE scope=:scope AND idempotency_key=:key_digest"
            ),
            {
                "raw_key": raw_key,
                "legacy_request_hash": legacy_request_hash,
                "scope": f"conversations:{goal.conversation_id}:turns",
                "key_digest": key_digest,
            },
        ).rowcount
    assert changed == 1

    changed_http_request = HttpConversationTurnCreate(
        content="改用同一个 key 请求不同内容。", project_id=goal.project_id
    )
    changed_request = to_application_message(changed_http_request, ConversationTurnCreate)
    with pytest.raises(ConflictError) as conflict:
        asyncio.run(
            service.conversations.send_conversation_turn(
                goal.conversation_id, changed_request, raw_key
            )
        )
    assert conflict.value.code == "idempotency_key_reused"

    replay = asyncio.run(
        service.conversations.send_conversation_turn(
            goal.conversation_id,
            to_application_message(original_http_request, ConversationTurnCreate),
            raw_key,
        )
    )

    assert replay == first
    provider.assert_awaited_once()
    with service.database.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM idempotency_records WHERE idempotency_key=:key"),
                {"key": raw_key},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM idempotency_records WHERE idempotency_key=:key"),
                {"key": key_digest},
            ).scalar_one()
            == 1
        )


def test_failed_new_analysis_goal_keeps_key_bound_to_original_request_identity(
    service, source_root, work_root, monkeypatch
):
    goal, _role_task, _run, _payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    raw_key = "failed-analysis-goal-private-key"
    first_request = ConversationTurnCreate(
        content="为所选外部指标建立独立目标。",
        project_id=goal.project_id,
        external_analysis_import_id=goal.source_external_import_id,
        start_new_analysis_goal=True,
    )
    work_dialogue = AsyncMock(
        side_effect=ConflictError("synthetic_prepare_failure", "模拟 root 创建后的准备失败。")
    )
    monkeypatch.setattr(service.work_dialogue, "respond", work_dialogue)

    with pytest.raises(ConflictError) as first_failure:
        asyncio.run(
            service.conversations.send_conversation_turn(
                goal.conversation_id, first_request, raw_key
            )
        )

    assert first_failure.value.code == "synthetic_prepare_failure"
    assert work_dialogue.await_count == 1
    assert _goal_count(service, goal.conversation_id) == 2

    changed_request = first_request.model_copy(
        update={"content": "使用同一 key 请求另一个分析问题。"}
    )
    with pytest.raises(ConflictError) as identity_conflict:
        asyncio.run(
            service.conversations.send_conversation_turn(
                goal.conversation_id, changed_request, raw_key
            )
        )

    assert identity_conflict.value.code == "analysis_goal_identity_conflict"
    assert work_dialogue.await_count == 1
    assert _goal_count(service, goal.conversation_id) == 2


def test_analysis_only_selection_restores_frozen_plan_run_without_outbox_lookup(
    service, source_root, work_root, monkeypatch
):
    goal, _role_task, run, _payload = _external_analysis_target(
        service, source_root, work_root, monkeypatch
    )
    before = service.conversations.get_conversation(goal.conversation_id)
    monkeypatch.setattr(
        service.automation,
        "get_analysis_outbox",
        lambda *_args, **_kwargs: pytest.fail("analysis-only selection has no proposal outbox"),
    )
    selected = service.conversations.select_composite_goal(
        goal.conversation_id,
        goal.intent_task_id,
        CompositeGoalSelection(expected_version=before.version),
        "analysis-only-frozen-selection",
    )

    role = next(item for item in selected.work_tasks if item.agent_role is WorkAgentRole.ANALYSIS)
    assert role.analysis_intent_task_id == goal.intent_task_id
    assert role.plan_revision_id is not None
    assert role.run_id == run.run_id
    assert service.automation.get_task(role.work_task_id).status in {
        TaskStatus.WAITING_RUN,
        TaskStatus.WAITING_DEPENDENCY,
    }

    provider = AsyncMock(return_value=generated("conversation", answer="仅解释已核验结果。"))
    monkeypatch.setattr(service.models, "_generate_rsfmri_chat", provider)
    before_budget = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    response = asyncio.run(
        service.conversations.send_conversation_turn(
            goal.conversation_id,
            ConversationTurnCreate(
                content="请解释已完成统计结果。",
                project_id=goal.project_id,
                agent_role=WorkAgentRole.ANALYSIS,
            ),
            "analysis-only-frozen-readout",
        )
    )

    assert response.assistant_message.payload["frozen_analysis_readout"]["read_only"] is True
    provider.assert_awaited_once()
    after_budget = service.automation.get_composite_goal_model_budget(goal.intent_task_id)
    assert after_budget.used_model_calls == before_budget.used_model_calls + 1
    assert service.automation.get_task(role.work_task_id).status == TaskStatus.WAITING_RUN
