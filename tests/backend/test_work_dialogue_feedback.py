from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationMessageView,
    ConversationRole,
    ConversationTurnCreate,
    WorkReviewCreate,
)
from neuroagent.application.contracts.plans import SkillPlanResolveRequest
from neuroagent.application.contracts.projects import DatasetCreate, ManifestScanRequest
from neuroagent.application.conversation_work import WorkActionResult
from neuroagent.application.errors import ApplicationError
from neuroagent.application.preflight_adapter import (
    planning_feedback_for_input_conflict,
)
from neuroagent.application.services import ApplicationServices
from neuroagent.application.work_draft import WorkDraft, draft_hash, preflight_skill_intent
from neuroagent.context.interfaces import ContextPacket
from neuroagent.multi_agent.registry import WorkAgentRole
from tests.backend.conftest import make_bids_dataset, make_project


def _complete_preprocessing_draft():
    from tests.backend.test_science_agent_api import _minimal_preprocessing_payload

    values = _minimal_preprocessing_payload()
    values.pop("provenance")
    return WorkDraft(
        preprocessing=values,
        primary_outputs=["preprocessed_bold"],
        qc_mode="skip",
    )


def _bound_compiled_review_candidate(service, source_root, work_root):
    project = make_project(service, source_root, work_root, key="review-recheck-project")
    bids = make_bids_dataset(source_root)
    dataset = service.projects.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Review recheck BIDS",
            source_path=str(bids),
            expected_project_version=project.version,
        ),
        "review-recheck-dataset",
    )
    manifest = service.projects.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "review-recheck-manifest",
    )
    project = service.projects.get_project(project.project_id)
    dataset = service.projects.get_dataset(dataset.dataset_id)
    draft = _complete_preprocessing_draft()
    request = SkillPlanResolveRequest(
        request=preflight_skill_intent(
            draft,
            project_id=project.project_id,
            dataset_ref=dataset.dataset_id,
            input_manifest_hash=manifest.content_hash,
            structural_artifact_id=f"manifest:{manifest.manifest_id}:anatomical-source",
        ),
        expected_project_version=project.version,
    )
    dry_run = service.skills.dry_run_skill_plan(request)
    assert dry_run.ready
    resolved = service.skills.resolve_skill_plan(request, "review-recheck-plan")
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id, workspace_path=str(bids)),
        "review-recheck-conversation",
    )
    payload = {
        "project": project.model_dump(mode="json"),
        "dataset": dataset.model_dump(mode="json"),
        "manifest": manifest.model_dump(mode="json"),
        "work_draft": {
            "draft": draft.model_dump(mode="json"),
            "hash": draft_hash(draft),
            "workspace_path": str(bids),
            "compiled": True,
            "facts": {},
            "sources": {},
            "planning_preflight": [],
        },
        "plan_revision": resolved.plan_revision.model_dump(mode="json"),
        "skill_dry_run": {
            "scope": dry_run.scope,
            "environment_probed": dry_run.environment_probed,
            "ready": dry_run.ready,
            "manifest_hash": dry_run.manifest_hash,
            "manifest_id": dry_run.manifest_id,
            "plan_hash": dry_run.plan_hash,
            "authority_revision_version": 1,
            "project_id": project.project_id,
            "project_version": project.version,
            "dataset_id": dataset.dataset_id,
            "draft_hash": draft_hash(draft),
        },
    }
    conversation, _, _, _ = service.conversations.repository.append_conversation_exchange(
        conversation.conversation_id,
        user_content="准备方案审查",
        assistant_content="只读预检通过。",
        assistant_payload=payload,
        tool=None,
        workspace_path=str(bids),
        preferred_profile_id=None,
        project_id=project.project_id,
        active_run_id=None,
    )
    return conversation, project, dataset, manifest, draft, resolved.plan_revision


def _plan_approval_run_counts(service):
    with service.database.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT (SELECT COUNT(*) FROM plan_revisions), "
                "(SELECT COUNT(*) FROM approval_records), "
                "(SELECT COUNT(*) FROM workflow_runs)"
            )
        ).one()


def _workspace_file_state(path):
    return sorted(
        (str(item.relative_to(path)), item.stat().st_size, item.stat().st_mtime_ns)
        for item in path.rglob("*")
        if item.is_file()
    )


def test_dry_run_compile_conflict_maps_to_stable_structured_issue() -> None:
    first = planning_feedback_for_input_conflict(
        "revision_conflict",
        "Project version changed; refresh and retry.",
        draft_revision="a" * 64,
        manifest_revision="b" * 64,
    )
    repeated = planning_feedback_for_input_conflict(
        "revision_conflict",
        "Project version changed; refresh and retry.",
        draft_revision="a" * 64,
        manifest_revision="b" * 64,
    )

    assert first[0].code == repeated[0].code
    assert first[0].fingerprint == repeated[0].fingerprint
    assert first[0].kind.value == "conflicting_evidence"
    assert first[1].disposition.value == "evidence_conflict"
    assert first[0].requires_user_decision is True


def test_work_review_accepts_unchanged_skill_dry_run_authority(
    service: ApplicationServices,
    source_root,
    work_root,
) -> None:
    conversation, _, _, _, draft, _ = _bound_compiled_review_candidate(
        service, source_root, work_root
    )
    payload = conversation.messages[-1].payload

    issues = service.conversations._work_review_authority_feedback(
        conversation,
        payload["work_draft"],
        draft,
        payload["plan_revision"],
    )

    assert issues == ()
    changed_draft = draft.model_copy(update={"qc_mode": "head_motion"})
    mismatch = service.conversations._work_review_authority_feedback(
        conversation,
        payload["work_draft"],
        changed_draft,
        payload["plan_revision"],
    )
    assert mismatch[0]["code"] == "skill_preflight_candidate_draft_changed_after_compile"
    assert mismatch[0]["target"] == "preprocessing_draft"
    assert mismatch[0]["disposition"] == "evidence_conflict"
    refreshed = service.conversations._work_review_authority_feedback(
        conversation,
        {
            **payload["work_draft"],
            "planning_preflight": [
                {
                    "code": "skill_preflight_input_conflict_previous",
                    "kind": "conflicting_evidence",
                    "evidence": [{"key": "work_draft", "revision": draft_hash(draft)}],
                }
            ],
        },
        draft,
        payload["plan_revision"],
    )
    assert refreshed == ()
    blocked_conversation = conversation.model_copy(
        update={
            "messages": [
                conversation.messages[-1].model_copy(
                    update={
                        "payload": {
                            **payload,
                            "skill_dry_run": {**payload["skill_dry_run"], "ready": False},
                        }
                    }
                )
            ]
        }
    )
    blocked = service.conversations._work_review_authority_feedback(
        blocked_conversation, payload["work_draft"], draft, payload["plan_revision"]
    )
    assert blocked[0]["code"] == "skill_preflight_not_ready"
    assert blocked[0]["disposition"] == "ask_user"
    before = _plan_approval_run_counts(service)
    reviewed = service.conversations.prepare_work_review(
        conversation.conversation_id,
        WorkReviewCreate(base_hash=draft_hash(draft)),
        "review-recheck-stable",
    )
    assert (
        reviewed.assistant_message.payload["plan_revision"]["plan_revision_id"]
        == payload["plan_revision"]["plan_revision_id"]
    )
    assert reviewed.assistant_message.payload["confirmation_request"]["options"]
    assert _plan_approval_run_counts(service) == before


@pytest.mark.parametrize(
    ("changed_authority", "expected_target", "expected_code"),
    [
        (
            "project",
            "project_revision",
            "skill_preflight_project_revision_changed_after_compile",
        ),
        (
            "manifest",
            "input_manifest",
            "skill_preflight_manifest_revision_changed_after_compile",
        ),
    ],
)
def test_work_review_rejects_changed_authority_without_plan_approval_or_run(
    service: ApplicationServices,
    source_root,
    work_root,
    monkeypatch,
    changed_authority: str,
    expected_target: str,
    expected_code: str,
) -> None:
    conversation, project, dataset, _, draft, _ = _bound_compiled_review_candidate(
        service, source_root, work_root
    )
    if changed_authority == "project":
        current_project = service.projects.get_project(project.project_id)
        service.projects.create_dataset(
            project.project_id,
            DatasetCreate(
                name="Concurrent project revision",
                source_path=dataset.source_path,
                expected_project_version=current_project.version,
            ),
            "review-recheck-project-change",
        )
    else:
        current_dataset = service.projects.get_dataset(dataset.dataset_id)
        service.projects.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=current_dataset.version),
            "review-recheck-manifest-change",
        )

    counts_before = _plan_approval_run_counts(service)
    workspace_before = _workspace_file_state(Path(dataset.source_path))
    monkeypatch.setattr(
        service.work_dialogue,
        "_compile",
        lambda *args, **kwargs: pytest.fail("stale review must not compile a Plan"),
    )

    result = service.conversations.prepare_work_review(
        conversation.conversation_id,
        WorkReviewCreate(base_hash=draft_hash(draft)),
        f"review-recheck-{changed_authority}",
    )

    payload = result.assistant_message.payload
    issue = payload["planning_preflight"][0]
    assert issue["code"] == expected_code
    assert issue["kind"] == "conflicting_evidence"
    assert issue["target"] == expected_target
    assert issue["disposition"] == "evidence_conflict"
    assert issue["requires_user_decision"] is True
    assert issue["retryable"] is False
    assert issue["repairs"] == [
        {"action": "ask_user", "boundary": "user_decision", "target": expected_target}
    ]
    assert "confirmation_request" not in payload
    assert payload["skill_dry_run"]["ready"] is False
    assert _plan_approval_run_counts(service) == counts_before
    assert _workspace_file_state(Path(dataset.source_path)) == workspace_before


def test_analysis_missing_design_choice_stops_without_inventing_a_retry(
    service: ApplicationServices,
) -> None:
    service.work_dialogue.model_budget = AgentLoopBudget(max_steps=4, max_total_tokens=20_000)
    dry_run_calls: list[dict[str, object]] = []
    service.work_dialogue.dry_run_statistical_proposal = lambda **kwargs: (
        dry_run_calls.append(kwargs) or (None, ())
    )
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
    assert dry_run_calls == []
    saved = result.payload["analysis_draft"]
    assert saved["draft"]["test"] == "one_sample_t"
    assert any(issue["disposition"] == "ask_user" for issue in saved["planning_preflight"]), [
        (issue["message"], issue["disposition"]) for issue in saved["planning_preflight"]
    ]
    assert "planning_feedback_budget" not in result.payload
    assert "statistical_design_proposal" not in result.payload


def test_analysis_provider_failure_keeps_a_controlled_error_envelope(
    service: ApplicationServices,
) -> None:
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "feedback-analysis-provider-error"
    )
    service.work_dialogue._interpret = AsyncMock(
        side_effect=ApplicationError(
            "work_model_unavailable", "数据分析 Agent 暂时不可用。", status_code=503
        )
    )

    result = asyncio.run(
        service.work_dialogue._respond_analysis(
            conversation,
            ConversationTurnCreate(content="分析 ALFF"),
            ContextPacket(
                question="分析 ALFF",
                metadata={"token_budget": 12_000, "selected": {}},
            ),
            workspace_path=None,
            project_id=None,
            preferred_profile_id=None,
            workspace_content_consent=False,
            idempotency_key="feedback-analysis-provider-error-turn",
            has_profiles=True,
            reply=lambda text, payload: WorkActionResult(
                assistant_content=text, payload=payload or {}
            ),
        )
    )

    assert service.work_dialogue._interpret.await_count == 1
    assert "model_error" in result.payload
    assert result.payload["model_error"]["code"] == "work_model_unavailable"
    assert "tool_loop" not in result.payload
    assert "数据分析 Agent 未能完成本轮请求" in result.assistant_content


def test_analysis_lineage_dry_run_blocker_is_user_visible_and_stops_once(
    service: ApplicationServices,
) -> None:
    from neuroagent.application.statistics_draft import DesignIssue

    service.work_dialogue.model_budget = AgentLoopBudget(max_steps=4, max_total_tokens=20_000)
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "feedback-analysis-lineage-chat"
    )
    proposed = {
        "answer": "整理统计草稿",
        "action": "prepare_statistical_analysis",
        "analysis_patch": {
            "test": "one_sample_t",
            "one_sample_baseline": 0.0,
            "metric_artifact_type": "metric.synthetic.alff",
            "tail": "two_sided",
            "missing_value_policy": "error",
            "correction_method": "none",
        },
    }
    dry_run_calls: list[dict[str, object]] = []

    async def interpret(context, **kwargs):
        return (
            json.dumps(proposed),
            {"request_budget": {"model_steps": 1, "total_tokens": 300}},
            None,
        )

    def dry_run(**kwargs):
        dry_run_calls.append(kwargs)
        return None, (
            DesignIssue(
                code="statistics_artifact_manifest_mismatch",
                message="登记的统计 Artifact 与来源 manifest 不一致。",
            ),
        )

    service.work_dialogue._interpret = AsyncMock(side_effect=interpret)
    service.work_dialogue.dry_run_statistical_proposal = dry_run
    service.work_dialogue.get_result_facts = None
    result = asyncio.run(
        service.work_dialogue._respond_analysis(
            conversation,
            ConversationTurnCreate(content="分析 ALFF", target_run_id="synthetic-run"),
            ContextPacket(
                question="分析 ALFF",
                metadata={"token_budget": 12_000, "selected": {}},
            ),
            workspace_path=None,
            project_id=None,
            preferred_profile_id=None,
            workspace_content_consent=False,
            idempotency_key="feedback-analysis-lineage-turn",
            has_profiles=True,
            reply=lambda text, payload: WorkActionResult(
                assistant_content=text, payload=payload or {}
            ),
        )
    )

    assert service.work_dialogue._interpret.await_count == 1
    assert len(dry_run_calls) == 1
    assert dry_run_calls[0]["run_id"] == "synthetic-run"
    assert result.payload["statistical_design_dry_run"]["status"] == "blocked"
    assert result.payload["statistical_design_dry_run"]["execution_ready"] is False
    assert result.payload["statistical_design_dry_run"]["environment_probed"] is False
    assert "statistical_design_proposal" not in result.payload
    preflight = result.payload["planning_preflight"]
    assert preflight[0]["kind"] == "conflicting_evidence"
    assert preflight[0]["disposition"] == "evidence_conflict"


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


def test_two_session_preprocessing_dry_run_blocks_with_capability_feedback(
    service: ApplicationServices,
    source_root,
    work_root,
) -> None:
    from tests.backend.conftest import make_project

    project = make_project(service, source_root, work_root, key="two-session-dry-run-project")
    bids = source_root / "bids-two-sessions-work"
    (bids / "dataset_description.json").parent.mkdir(parents=True)
    (bids / "dataset_description.json").write_text(
        '{"Name":"synthetic","BIDSVersion":"1.9.0"}', encoding="utf-8"
    )
    for session in ("ses-01", "ses-02"):
        subject = "sub-01"
        functional = bids / subject / session / "func"
        anatomical = bids / subject / "anat"
        functional.mkdir(parents=True)
        anatomical.mkdir(parents=True, exist_ok=True)
        (functional / f"{subject}_{session}_task-rest_bold.nii.gz").write_bytes(b"synthetic-bold")
        (anatomical / f"{subject}_T1w.nii.gz").write_bytes(b"synthetic-t1")
    dataset = service.projects.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Two-session BIDS",
            source_path=str(bids),
            expected_project_version=project.version,
        ),
        "two-session-dry-run-dataset",
    )
    manifest = service.projects.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "two-session-dry-run-manifest",
    )
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id),
        "two-session-dry-run-chat",
    )
    binding = ConversationMessageView(
        message_id="two-session-dry-run-binding",
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
        plans_before = connection.execute(text("SELECT COUNT(*) FROM plan_revisions")).scalar_one()

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
            idempotency_key="two-session-dry-run-turn",
            has_profiles=True,
        )
    )

    assert service.work_dialogue._interpret.await_count == 1
    assert result.commit is None
    assert result.payload["skill_dry_run"]["ready"] is False
    assert result.payload["skill_dry_run"]["environment_probed"] is False
    issue = next(
        item
        for item in result.payload["skill_dry_run"]["issues"]
        if item["code"] == "MANIFEST_PREPROCESSING_SESSION_UNSUPPORTED"
    )
    assert issue["path"] == "manifest.subjects.session_id"
    feedback = next(
        item
        for item in result.payload["planning_preflight"]
        if item["kind"] == "unsupported_capability"
    )
    assert feedback["disposition"] == "blocked_capability"
    assert feedback["target"] == "manifest.subjects.session_id"
    assert "请将每个 session 分别整理为独立输入" in feedback["message"]
    with service.database.engine.connect() as connection:
        plans_after = connection.execute(text("SELECT COUNT(*) FROM plan_revisions")).scalar_one()
    assert plans_after == plans_before
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
