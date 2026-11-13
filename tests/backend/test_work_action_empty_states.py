from __future__ import annotations

import pytest

from neuroagent.application.contracts.conversations import (
    ConversationAction,
    ConversationTurnCreate,
)


@pytest.mark.parametrize(
    ("action", "expected_text", "expected_tool"),
    [
        (ConversationAction.GET_PROGRESS, "当前对话尚未启动运行。", None),
        (
            ConversationAction.GET_QC_STATUS,
            "当前运行还没有生成 QC 审核结果",
            "get_qc_status",
        ),
        (
            ConversationAction.CREATE_HEAD_MOTION_QC,
            "请先指定一个已完成 DPABI realign 的运行",
            "create_head_motion_qc",
        ),
        (
            ConversationAction.APPROVE_QC_REVIEW,
            "当前对话没有可批准的 QC 审核结果。",
            "approve_qc_review",
        ),
        (ConversationAction.SKIP_QC, "请先指定一个已完成计算、等待 QC 的运行。", None),
    ],
)
def test_work_actions_explain_missing_run_or_qc_state_without_side_effects(
    service, action: ConversationAction, expected_text: str, expected_tool: str | None
) -> None:
    result = service.conversation_work.execute(
        ConversationTurnCreate(content="请检查当前进度", action=action),
        workspace_path=None,
        active_run_id=None,
        project_id=None,
        idempotency_key=f"empty-state-{action.value}",
    )

    assert expected_text in result.assistant_content
    assert result.active_run_id is None
    assert result.payload == {}
    if expected_tool is None:
        assert result.tool is None
    else:
        assert result.tool is not None
        assert result.tool["tool_name"] == expected_tool
        assert result.tool["status"] == "awaiting_confirmation"
    assert service.repository.list_runs() == []
