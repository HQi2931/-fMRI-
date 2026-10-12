from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationTurnCreate,
)
from neuroagent.application.conversation_work import WorkActionResult
from neuroagent.application.services import ApplicationServices
from neuroagent.context.interfaces import ContextPacket


def test_analysis_missing_design_choice_stops_without_inventing_a_retry(
    service: ApplicationServices,
) -> None:
    service.work_dialogue.model_budget = AgentLoopBudget(max_steps=4, max_total_tokens=20_000)
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "feedback-analysis-chat"
    )
    proposed = {
        "answer": "整理统计草稿",
        "action": "prepare_statistical_analysis",
        "analysis_patch": {"test": "one_sample_t"},
    }

    async def interpret(context, **kwargs):
        return (
            json.dumps(proposed),
            {"request_budget": {"model_steps": 1, "total_tokens": 300}},
            None,
        )

    service.work_dialogue._interpret = AsyncMock(side_effect=interpret)
    result = asyncio.run(
        service.work_dialogue._respond_analysis(
            conversation,
            ConversationTurnCreate(content="分析 age 与 ALFF 的相关"),
            ContextPacket(
                question="分析 age 与 ALFF 的相关",
                metadata={"token_budget": 12_000, "selected": {}},
            ),
            workspace_path=None,
            project_id=None,
            preferred_profile_id=None,
            workspace_content_consent=False,
            idempotency_key="feedback-analysis-turn",
            has_profiles=True,
            reply=lambda text, payload: WorkActionResult(
                assistant_content=text, payload=payload or {}
            ),
        )
    )

    assert service.work_dialogue._interpret.await_count == 1
    saved = result.payload["analysis_draft"]
    assert saved["draft"]["test"] == "one_sample_t"
    assert any(issue["disposition"] == "ask_user" for issue in saved["planning_preflight"]), [
        (issue["message"], issue["disposition"]) for issue in saved["planning_preflight"]
    ]
    assert "planning_feedback_budget" not in result.payload
    assert "statistical_design_proposal" not in result.payload


def test_preprocessing_feedback_retries_safe_patch_with_remaining_budget(
    service: ApplicationServices,
) -> None:
    from tests.backend.test_work_draft import complete_draft

    service.work_dialogue.model_budget = AgentLoopBudget(max_steps=4, max_total_tokens=20_000)
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "feedback-preprocessing-chat"
    )
    draft = complete_draft().model_dump(mode="json", exclude={"confirmed_hash"})
    draft["requested_metrics"] = ["alff", "falff"]
    draft["primary_outputs"] = ["raw_alff", "raw_falff"]
    draft["alff_falff"] = {
        "tr_seconds": 2.0,
        "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
        # This inconsistency is confined to a model-proposed, unapproved draft.
        "requested_metrics": ["alff"],
        "requested_scalings": ["raw"],
        "mask_source": "dpabi_auto",
        "mask_artifact_id": None,
        "filter_timing": "disabled",
        "result_smoothing": False,
        "result_smoothing_fwhm_mm": None,
    }
    proposed = {"answer": "准备方案", "action": "prepare_preprocessing_plan", "draft_patch": draft}
    repaired = {
        "answer": "准备方案",
        "action": "prepare_preprocessing_plan",
        "draft_patch": {"alff_falff": {"requested_metrics": ["alff", "falff"]}},
    }
    contexts: list[dict] = []
    budgets: list[AgentLoopBudget] = []

    async def interpret(context, **kwargs):
        contexts.append(context)
        budgets.append(kwargs["budget_override"])
        response = proposed if len(contexts) == 1 else repaired
        return (
            json.dumps(response),
            {"request_budget": {"model_steps": 1, "total_tokens": 300}},
            None,
        )

    service.work_dialogue._interpret = AsyncMock(side_effect=interpret)
    result = asyncio.run(
        service.work_dialogue.respond(
            conversation,
            ConversationTurnCreate(content="准备预处理方案"),
            ContextPacket(
                question="准备预处理方案",
                metadata={"token_budget": 12_000, "selected": {}},
            ),
            workspace_path=None,
            project_id=None,
            preferred_profile_id=None,
            idempotency_key="feedback-preprocessing-turn",
            has_profiles=True,
        )
    )

    assert service.work_dialogue._interpret.await_count == 2
    assert budgets[1].max_steps < budgets[0].max_steps
    assert budgets[1].max_total_tokens < budgets[0].max_total_tokens
    assert contexts[1]["work_context"]["planning_preflight"]
    saved = result.payload["work_draft"]["draft"]
    assert saved["requested_metrics"] == ["alff", "falff"]
    assert saved["alff_falff"]["requested_metrics"] == ["alff", "falff"]
    assert result.payload["model"]["planning_feedback_budget"]["rounds"] == 2
