"""The Work tier boundary: what a model may name, and what it may never name.

These tests are the regression guard for the safety property rather than for a
behaviour: a privileged action becoming model-selectable is the failure mode, and
it must break a test instead of reaching a queued run.
"""

from __future__ import annotations

import pytest

from neuroagent.agent.tool_spec import MODEL_VISIBLE_TIERS, ToolTier
from neuroagent.application.contracts.conversations import (
    ConversationAction,
)
from neuroagent.application.work_actions import (
    MODEL_SELECTABLE_WORK_ACTIONS,
    SERVER_ONLY_ACTIONS,
    WORK_ACTION_DISPATCH,
    WORK_MODEL_ACTIONS,
    WorkActionError,
    check_tier_boundary,
    dispatch_target,
    is_model_selectable,
)
from neuroagent.application.work_dialogue import WorkInterpretation
from neuroagent.application.work_prompts import _WORK_ACTIONS, _WORK_SYSTEM_PROMPT


def test_no_server_only_action_is_model_selectable() -> None:
    assert frozenset() == MODEL_SELECTABLE_WORK_ACTIONS & SERVER_ONLY_ACTIONS


def test_every_model_action_is_read_or_propose_tier() -> None:
    assert frozenset(WORK_MODEL_ACTIONS) == MODEL_SELECTABLE_WORK_ACTIONS
    for tier in WORK_MODEL_ACTIONS.values():
        assert tier in MODEL_VISIBLE_TIERS
    assert ToolTier.EXECUTE not in frozenset(WORK_MODEL_ACTIONS.values())


def test_start_preprocessing_is_server_only() -> None:
    assert ConversationAction.START_PREPROCESSING.value in SERVER_ONLY_ACTIONS
    assert is_model_selectable(ConversationAction.START_PREPROCESSING.value) is False


def test_dispatch_table_only_points_at_selectable_actions() -> None:
    assert set(WORK_ACTION_DISPATCH).issubset(MODEL_SELECTABLE_WORK_ACTIONS)
    # The propose-only action renders a command and must never dispatch.
    assert dispatch_target("propose_run_confirmation") is None
    assert dispatch_target("conversation") is None
    assert dispatch_target("get_progress") is ConversationAction.GET_PROGRESS


@pytest.mark.parametrize("action", sorted(SERVER_ONLY_ACTIONS))
def test_model_interpretation_rejects_server_only_actions(action: str) -> None:
    with pytest.raises(ValueError, match="not available to a model"):
        WorkInterpretation(answer="ok", action=action)


@pytest.mark.parametrize("action", sorted(MODEL_SELECTABLE_WORK_ACTIONS))
def test_model_interpretation_accepts_every_selectable_action(action: str) -> None:
    assert WorkInterpretation(answer="ok", action=action).action == action


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="not available to a model"):
        WorkInterpretation(answer="ok", action="run_shell")


def test_work_prompt_advertises_exactly_the_selectable_actions() -> None:
    """The prompt is the model's contract, so it must not drift from the set.

    The application prompt reads its actions from the same policy registry used
    by server-side validation.
    """

    assert set(_WORK_ACTIONS) == MODEL_SELECTABLE_WORK_ACTIONS
    assert len(_WORK_ACTIONS) == len(set(_WORK_ACTIONS)), "prompt lists an action twice"


def test_work_prompt_does_not_advertise_server_only_actions() -> None:
    for action in SERVER_ONLY_ACTIONS:
        assert action not in _WORK_SYSTEM_PROMPT


def test_work_prompt_does_not_request_a_removed_protocol_reference() -> None:
    assert "study_protocol_ref" not in _WORK_SYSTEM_PROMPT
    assert "研究方案引用" not in _WORK_SYSTEM_PROMPT


def test_tier_check_rejects_a_leaked_privileged_action() -> None:
    """The guard must actually fire when a privileged action leaks in."""

    leaked_actions = {**WORK_MODEL_ACTIONS, "start_preprocessing": ToolTier.PROPOSE}
    with pytest.raises(WorkActionError, match="server-only actions must never be model-selectable"):
        check_tier_boundary(model_actions=leaked_actions)


def test_tier_check_rejects_an_execute_tier_label() -> None:
    with pytest.raises(WorkActionError, match="read/propose tier only"):
        check_tier_boundary(
            model_actions={"conversation": ToolTier.EXECUTE},
            server_only=frozenset(),
        )


def test_tier_check_rejects_a_dispatch_entry_for_an_unknown_action() -> None:
    with pytest.raises(WorkActionError, match="non-selectable actions"):
        check_tier_boundary(dispatch={"run_shell": ConversationAction.GET_PROGRESS})
