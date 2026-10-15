"""User-facing CompositeGoal summaries require exact persisted intent evidence."""

from neuroagent.application.automation.composite_goal import (
    CompositeGoalIdentity,
    CompositeGoalIntentMessage,
    CompositeGoalSnapshot,
    project_composite_goal,
)
from neuroagent.application.hashing import content_hash


def _goal(request: str) -> CompositeGoalIdentity:
    return CompositeGoalIdentity(
        intent_task_id="intent-opaque-id",
        conversation_id="conversation-1",
        project_id="project-1",
        analysis_request_hash=content_hash(request),
        preprocessing_task_id="prep-task-1",
    )


def _message(
    *,
    request: str,
    task_id: str = "intent-opaque-id",
    conversation_id: str = "conversation-1",
    project_id: str = "project-1",
) -> CompositeGoalIntentMessage:
    return CompositeGoalIntentMessage(
        conversation_id=conversation_id,
        role="assistant",
        payload={
            "analysis_dependency": {
                "task_kind": "preprocess_then_analyze",
                "task_id": task_id,
                "conversation_id": conversation_id,
                "project_id": project_id,
                "analysis_request": request,
            }
        },
    )


def _summary(request: str, *messages: CompositeGoalIntentMessage) -> str | None:
    return project_composite_goal(
        CompositeGoalSnapshot(goal=_goal(request), analysis_intent_messages=messages)
    ).analysis_summary


def test_summary_uses_exact_request_and_collapses_whitespace() -> None:
    request = "Compare\tALFF\n between groups using the selected preprocessing."

    summary = _summary(request, _message(request=request))

    assert summary == "Compare ALFF between groups using the selected preprocessing."
    assert "intent-opaque-id" not in (summary or "")


def test_summary_is_bounded() -> None:
    request = "Compare ALFF " + "between groups " * 30

    summary = _summary(request, _message(request=request))

    assert summary is not None
    assert len(summary) <= 160
    assert summary.endswith("…")


def test_summary_is_null_when_exact_identity_or_hash_cannot_be_verified() -> None:
    request = "Compare ALFF between groups."

    assert _summary(request, _message(request=request, task_id="another-intent")) is None
    assert (
        _summary(
            request,
            _message(request=request, conversation_id="another-conversation"),
        )
        is None
    )
    assert _summary(request, _message(request=request, project_id="another-project")) is None
    assert _summary(request, _message(request="different request")) is None
    assert (
        _summary(
            request,
            _message(request=request),
            _message(request=request),
        )
        is None
    )
