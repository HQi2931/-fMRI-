from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from sqlalchemy import text

from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationMessageView,
    ConversationRole,
    ConversationTurnCreate,
)
from neuroagent.application.contracts.projects import DatasetCreate, ManifestScanRequest
from neuroagent.application.conversation_work import WorkActionResult
from neuroagent.application.services import ApplicationServices
from neuroagent.context.interfaces import ContextPacket
from neuroagent.multi_agent.registry import WorkAgentRole


def _complete_preprocessing_draft():
    from neuroagent.application.work_draft import WorkDraft
    from tests.backend.test_science_agent_api import _minimal_preprocessing_payload

    values = _minimal_preprocessing_payload()
    values.pop("provenance")
    return WorkDraft(
        preprocessing=values,
        primary_outputs=["preprocessed_bold"],
        qc_mode="skip",
    )


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


def test_preprocessing_scientific_patch_waits_for_user_decision(
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
    contexts: list[dict] = []
    budgets: list[AgentLoopBudget] = []

    async def interpret(context, **kwargs):
        contexts.append(context)
        budgets.append(kwargs["budget_override"])
        return (
            json.dumps(proposed),
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

    assert service.work_dialogue._interpret.await_count == 1
    assert len(budgets) == 1
    assert contexts[0]["work_context"]["planning_preflight"]
    saved = result.payload["work_draft"]["draft"]
    assert saved["requested_metrics"] == ["alff", "falff"]
    assert saved["alff_falff"]["requested_metrics"] == ["alff"]
    assert any(
        issue["disposition"] == "ask_user" and issue["kind"] == "scientific_decision_required"
        for issue in result.payload["planning_preflight"]
    )


def test_first_unpersisted_preprocessing_draft_runs_skill_dry_run(
    service: ApplicationServices,
    source_root,
    work_root,
) -> None:
    from tests.backend.conftest import make_bids_dataset, make_project

    project = make_project(service, source_root, work_root, key="first-dry-run-project")
    bids = make_bids_dataset(source_root)
    dataset = service.projects.create_dataset(
        project.project_id,
        DatasetCreate(
            name="First-turn BIDS",
            source_path=str(bids),
            expected_project_version=project.version,
        ),
        "first-dry-run-dataset",
    )
    manifest = service.projects.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "first-dry-run-manifest",
    )
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id),
        "first-dry-run-chat",
    )
    binding = ConversationMessageView(
        message_id="first-dry-run-binding",
        conversation_id=conversation.conversation_id,
        sequence=1,
        role=ConversationRole.ASSISTANT,
        content="已绑定预处理工作区输入。",
        payload={
            "project": service.projects.get_project(project.project_id).model_dump(mode="json"),
            "dataset": service.projects.get_dataset(dataset.dataset_id).model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
        },
        created_at=datetime.now(UTC),
    )
    conversation = conversation.model_copy(update={"messages": [binding]})
    proposed = {
        "answer": "已整理预处理方案草稿。",
        "action": "prepare_preprocessing_plan",
        "draft_patch": _complete_preprocessing_draft().model_dump(
            mode="json", exclude={"confirmed_hash"}
        ),
    }
    service.work_dialogue._interpret = AsyncMock(
        return_value=(
            json.dumps(proposed),
            {"request_budget": {"model_steps": 1, "total_tokens": 300}},
            None,
        )
    )
    with service.database.engine.connect() as connection:
        before = connection.execute(text("SELECT COUNT(*) FROM plan_revisions")).scalar_one()

    result = asyncio.run(
        service.work_dialogue.respond(
            conversation,
            ConversationTurnCreate(
                content="准备预处理方案",
                agent_role=WorkAgentRole.PREPROCESSING,
            ),
            ContextPacket(
                question="准备预处理方案",
                metadata={"token_budget": 12_000, "selected": {}},
            ),
            workspace_path=None,
            project_id=project.project_id,
            preferred_profile_id=None,
            idempotency_key="first-dry-run-turn",
            has_profiles=True,
        )
    )

    assert service.work_dialogue._interpret.await_count == 1
    assert result.commit is None
    assert result.payload["skill_dry_run"]["scope"] == "skill_contract_compile_only"
    assert result.payload["skill_dry_run"]["environment_probed"] is False
    with service.database.engine.connect() as connection:
        after = connection.execute(text("SELECT COUNT(*) FROM plan_revisions")).scalar_one()
    assert after == before
    assert service.repository.list_runs() == []


def test_skill_scientific_blocker_cannot_be_patched_by_feedback_model(
    service: ApplicationServices,
    source_root,
    work_root,
) -> None:
    from neuroagent.application.work_draft import draft_hash, draft_snapshot
    from neuroagent.skills.models import (
        IssueSeverity,
        SkillValidationIssue,
        ValidationReport,
    )
    from tests.backend.conftest import make_bids_dataset, make_project

    project = make_project(service, source_root, work_root, key="skill-blocker-project")
    bids = make_bids_dataset(source_root)
    dataset = service.projects.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Skill blocker BIDS",
            source_path=str(bids),
            expected_project_version=project.version,
        ),
        "skill-blocker-dataset",
    )
    manifest = service.projects.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "skill-blocker-manifest",
    )
    draft = _complete_preprocessing_draft()
    saved = draft_snapshot(draft, workspace_path=None, sources={})
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id),
        "skill-blocker-chat",
    )
    binding = ConversationMessageView(
        message_id="skill-blocker-binding",
        conversation_id=conversation.conversation_id,
        sequence=1,
        role=ConversationRole.ASSISTANT,
        content="已绑定预处理工作区输入和草稿。",
        payload={
            "agent_role": WorkAgentRole.PREPROCESSING.value,
            "work_draft": saved,
            "project": service.projects.get_project(project.project_id).model_dump(mode="json"),
            "dataset": service.projects.get_dataset(dataset.dataset_id).model_dump(mode="json"),
            "manifest": manifest.model_dump(mode="json"),
        },
        created_at=datetime.now(UTC),
    )
    conversation = conversation.model_copy(update={"messages": [binding]})
    monkeypatch_issue = SkillValidationIssue(
        code="METRIC_PREPROCESSING_SMOOTHING_CONFLICT",
        severity=IssueSeverity.BLOCKING,
        message="指标计算要求公共预处理保持未平滑。",
        path="preprocessing.smoothing.timing",
    )
    service.skills.skill_validator.validate_resolution = lambda _resolution: ValidationReport(
        issues=(monkeypatch_issue,)
    )
    proposed = {
        "answer": "已修正 TR 并准备方案。",
        "action": "prepare_preprocessing_plan",
        "draft_patch": {"preprocessing": {"tr_seconds": 2.5}},
    }
    service.work_dialogue._interpret = AsyncMock(
        return_value=(
            json.dumps(proposed),
            {"request_budget": {"model_steps": 1, "total_tokens": 300}},
            None,
        )
    )
    with service.database.engine.connect() as connection:
        plans_before = connection.execute(text("SELECT COUNT(*) FROM plan_revisions")).scalar_one()

    result = asyncio.run(
        service.work_dialogue.respond(
            conversation,
            ConversationTurnCreate(
                content="请修正 TR 并准备预处理方案",
                agent_role=WorkAgentRole.PREPROCESSING,
            ),
            ContextPacket(
                question="请修正 TR 并准备预处理方案",
                metadata={"token_budget": 12_000, "selected": {}},
            ),
            workspace_path=None,
            project_id=project.project_id,
            preferred_profile_id=None,
            idempotency_key="skill-blocker-turn",
            has_profiles=True,
        )
    )

    assert service.work_dialogue._interpret.await_count == 1
    assert result.commit is None
    assert result.payload["work_draft"]["hash"] == saved["hash"] == draft_hash(draft)
    assert result.payload["work_draft"]["draft"]["preprocessing"]["tr_seconds"] == 2.0
    assert result.payload["planning_preflight"][0]["disposition"] == "ask_user"
    assert result.payload["planning_preflight"][0]["kind"] == "scientific_decision_required"
    assert "科研方法或输入冲突需要你决定" in result.assistant_content
    with service.database.engine.connect() as connection:
        plans_after = connection.execute(text("SELECT COUNT(*) FROM plan_revisions")).scalar_one()
    assert plans_after == plans_before
