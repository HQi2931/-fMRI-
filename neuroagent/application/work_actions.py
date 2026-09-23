"""The tier boundary for Work actions a model may select.

This module is the single source of truth for "what may a model ask Work to do".
The safety property is structural rather than defensive: an action that starts,
approves or skips work is simply *absent* from the model-selectable set, so a
model cannot name it even if it tries, and a regression breaks an import-time
check and a test rather than reaching production.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from neuroagent.agent.tool_spec import MODEL_VISIBLE_TIERS, ToolTier
from neuroagent.application.contracts import ConversationAction


class WorkActionError(ValueError):
    pass


#: Every action the Work model may name, with the authority it carries.
#:
#: ``READ`` actions only observe state. ``PROPOSE`` actions write drafts and
#: proposals but never start work. ``propose_run_confirmation`` is PROPOSE, not
#: EXECUTE: it only renders the server-issued confirmation command, and the
#: server still refuses to queue anything until the user echoes that command.
WORK_MODEL_ACTIONS: Mapping[str, ToolTier] = MappingProxyType(
    {
        "conversation": ToolTier.READ,
        "get_progress": ToolTier.READ,
        "get_qc_status": ToolTier.READ,
        "get_statistical_results": ToolTier.READ,
        "prepare_preprocessing_plan": ToolTier.PROPOSE,
        "propose_run_confirmation": ToolTier.PROPOSE,
    }
)

MODEL_SELECTABLE_WORK_ACTIONS: frozenset[str] = frozenset(WORK_MODEL_ACTIONS)

#: Actions that only server-owned code paths may take: REST/UI calls and the
#: exact user confirmation command.  They are listed explicitly so the
#: import-time check below is meaningful rather than vacuous.
SERVER_ONLY_ACTIONS: frozenset[str] = frozenset(
    {
        ConversationAction.START_PREPROCESSING.value,
        ConversationAction.APPROVE_QC_REVIEW.value,
        ConversationAction.SKIP_QC.value,
        ConversationAction.CREATE_HEAD_MOTION_QC.value,
        ConversationAction.SETUP_WORKSPACE.value,
    }
)

#: Model action -> server dispatch.  ``propose_run_confirmation`` is deliberately
#: absent: it renders the confirmation command and never dispatches anything.
WORK_ACTION_DISPATCH: Mapping[str, ConversationAction] = MappingProxyType(
    {
        "get_progress": ConversationAction.GET_PROGRESS,
        "get_qc_status": ConversationAction.GET_QC_STATUS,
        "get_statistical_results": ConversationAction.GET_STATISTICAL_RESULTS,
        "prepare_preprocessing_plan": ConversationAction.PREPARE_PREPROCESSING_PLAN,
    }
)


def check_tier_boundary(
    model_actions: Mapping[str, ToolTier] | None = None,
    server_only: frozenset[str] | None = None,
    dispatch: Mapping[str, ConversationAction] | None = None,
) -> None:
    """Raise when a privileged action could be selected by a model.

    Pure and parameterised so the guard itself is testable: the real tier tables
    are immutable at runtime (which is desirable), so an unparameterised check
    could never be exercised with a deliberately leaked action.
    """

    actions = WORK_MODEL_ACTIONS if model_actions is None else model_actions
    privileged = SERVER_ONLY_ACTIONS if server_only is None else server_only
    routing = WORK_ACTION_DISPATCH if dispatch is None else dispatch

    leaked = set(actions) & privileged
    if leaked:
        raise WorkActionError(
            f"server-only actions must never be model-selectable: {sorted(leaked)}"
        )
    mislabelled = {name for name, tier in actions.items() if tier not in MODEL_VISIBLE_TIERS}
    if mislabelled:
        raise WorkActionError(
            f"model actions must be read/propose tier only: {sorted(mislabelled)}"
        )
    undispatched = set(routing) - set(actions)
    if undispatched:
        raise WorkActionError(
            f"dispatch table points at non-selectable actions: {sorted(undispatched)}"
        )


check_tier_boundary()


def is_model_selectable(action: str) -> bool:
    return action in MODEL_SELECTABLE_WORK_ACTIONS


def dispatch_target(action: str) -> ConversationAction | None:
    """Return the server action for a model action, or ``None`` when it only renders."""

    return WORK_ACTION_DISPATCH.get(action)
