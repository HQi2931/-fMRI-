from __future__ import annotations

from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
import pytest
from fastapi.testclient import TestClient

from neuroagent.api.app import create_app
from neuroagent.application.automation.models import TaskStatus, TaskStepStatus
from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationTurnCreate,
    WorkAgentSelection,
)
from neuroagent.application.contracts.external_imports import (
    ExternalAnalysisImportIncludeRequest,
    ExternalAnalysisImportInspectRequest,
)
from neuroagent.application.contracts.plans import (
    ApprovalCreate,
)
from neuroagent.application.contracts.projects import (
    ProjectCreate,
)
from neuroagent.application.contracts.statistics import (
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticalWorkContext,
    StatisticsRunCreate,
)
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.services import ApplicationServices
from neuroagent.application.statistics_draft import StatisticsDraft, statistics_draft_hash
from neuroagent.application.work_agents import work_agent_role_for_turn
from neuroagent.application.work_tasks import preprocessing_source_run, work_task
from neuroagent.bootstrap import build_service
from neuroagent.multi_agent.registry import WorkAgentRole

from .conftest import make_approved_plan
from .test_work_dialogue import configure, generated


def _proposal(service: ApplicationServices, source_root: Path, work_root: Path):
    source = source_root / "metrics"
    for group, subjects in (("ASD", ("sub-01", "sub-02")), ("HC", ("sub-03", "sub-04"))):
        (source / group).mkdir(parents=True)
        for index, subject in enumerate(subjects):
            nib.save(
                nib.Nifti1Image(np.full((3, 4, 5), index + 1, dtype=np.float32), np.eye(4)),
                source / group / f"zALFFMap_{subject}.nii",
            )
    nib.save(nib.Nifti1Image(np.ones((3, 4, 5), dtype=np.float32), np.eye(4)), source / "Mask.nii")
    project = service.projects.create_project(
        ProjectCreate(
            name="Work task acceptance",
            source_roots=[str(source_root)],
            work_root=str(work_root / "project"),
        ),
        "task-project",
    )
    inspected = service.external_imports.inspect_external_analysis_import(
        ExternalAnalysisImportInspectRequest(
            project_id=project.project_id, source_path=str(source)
        ),
        "task-inspect",
    )
    imported = service.external_imports.include_external_analysis_import(
        inspected.import_id,
        ExternalAnalysisImportIncludeRequest(
            expected_version=inspected.version,
            included_subject_ids=("sub-01", "sub-02", "sub-03", "sub-04"),
            group_order=("ASD", "HC"),
            coordinate_space="unknown",
            acknowledge_unknown_processing=True,
            acknowledge_candidate_mapping=True,
            mask_relative_path="Mask.nii",
            actor="researcher",
            reason="Synthetic input review",
        ),
        "task-include",
    )
    draft = StatisticsDraft(
        test="independent_two_sample_t",
        grouping_column="group",
        group_order=["ASD", "HC"],
        metric_artifact_type="external.alff.z_score",
        tail="two_sided",
        missing_value_policy="error",
        correction_method="fdr",
        correction_parameters={"q_threshold": 0.05},
        external_import_id=imported.import_id,
    )
    proposal, issues = service.statistics.prepare_work_statistical_design_proposal(
        external_import_id=imported.import_id, project_id=project.project_id, draft=draft
    )
    assert proposal is not None and not issues
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id, workspace_path=str(source)),
        "task-conversation",
    )
    payload = {
        "agent_role": "analysis",
        "analysis_draft": {
            "hash": statistics_draft_hash(draft),
            "draft": draft.model_dump(mode="json"),
        },
        "statistical_design_proposal": proposal.model_dump(mode="json"),
    }
    _append(service, conversation.conversation_id, payload)
    request = StatisticalDesignCreate(
        project_id=project.project_id,
        expected_project_version=proposal.project_version,
        input_manifest_hash=proposal.input_manifest_hash,
        design=proposal.design,
        correction=proposal.correction,
        work_context=StatisticalWorkContext(
            conversation_id=conversation.conversation_id, proposal_hash=proposal.proposal_hash
        ),
    )
    return conversation.conversation_id, request, payload


def _append(
    service: ApplicationServices,
    conversation_id: str,
    payload: dict[str, Any],
    run_id: str | None = None,
) -> None:
    conversation = service.conversations.get_conversation(conversation_id)
    service.repository.append_conversation_exchange(
        conversation_id,
        user_content="Synthetic review",
        assistant_content="Server proposal",
        assistant_payload=payload,
        tool=None,
        workspace_path=conversation.workspace_path,
        preferred_profile_id=None,
        project_id=conversation.project_id,
        active_run_id=run_id or conversation.active_run_id,
    )


def _approve(service: ApplicationServices, plan_id: str) -> None:
    plan = service.plans.get_plan(plan_id)
    service.plans.approve_plan(
        plan_id,
        ApprovalCreate(
            expected_version=plan.version,
            plan_hash=plan.plan_hash,
            actor="researcher",
            decision="approved",
            reason="Synthetic Work design review",
        ),
        "task-approve",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("has_model", [True, False])
async def test_analysis_readout_retains_workspace_and_formal_task_on_reload(
    service, source_root, work_root, monkeypatch, has_model
):
    conversation_id, request, _ = _proposal(service, source_root, work_root)
    frozen = service.statistics.create_statistical_design(request, "readout-create")
    plan_id = frozen.plan_revision.plan_revision_id
    service.statistics.validate_statistical_design(
        plan_id, StatisticalDesignValidationRequest(expected_version=1), "readout-validate"
    )
    _approve(service, plan_id)
    run = service.runs.create_statistics_run(
        StatisticsRunCreate(
            project_id=request.project_id,
            statistical_design_revision_id=plan_id,
            expected_plan_hash=frozen.plan_revision.plan_hash,
        ),
        "readout-run",
    )
    before = service.conversations.get_conversation(conversation_id)
    if has_model:
        configure(service, monkeypatch, [generated("get_statistical_results")])
    turn = await service.conversations.send_conversation_turn(
        conversation_id,
        ConversationTurnCreate(
            content="查看统计结果",
            preferred_profile_id="dialogue-model" if has_model else None,
            workspace_path=before.workspace_path,
        ),
        "readout-turn",
    )
    assert turn.tool_call.tool_name == "get_statistical_results"
    fresh = build_service(service.settings)
    try:
        restored = fresh.conversations.get_conversation(conversation_id)
        assert restored.workspace_path == before.workspace_path
        assert restored.active_run_id == run.run_id
        assert work_task(restored, WorkAgentRole.ANALYSIS) == work_task(
            before, WorkAgentRole.ANALYSIS
        )
        assert len(fresh.runs.list_runs(project_id=request.project_id)) == 1
    finally:
        fresh.close()


def test_work_statistics_persists_plan_run_and_role_without_browser_cache(
    service, source_root, work_root
):
    conversation_id, request, payload = _proposal(service, source_root, work_root)
    frozen = service.statistics.create_statistical_design(request, "task-create")
    plan_id = frozen.plan_revision.plan_revision_id
    context = frozen.plan_revision.plan["work_context"]
    conversation = service.conversations.get_conversation(conversation_id)
    task = work_task(conversation, WorkAgentRole.ANALYSIS)
    assert task.plan_revision_id == plan_id
    assert task.work_task_id is not None
    durable = service.automation.get_task(task.work_task_id)
    assert durable.status is TaskStatus.WAITING_USER
    assert durable.authority_refs.plan_revision_id == plan_id
    assert durable.steps[0].kind == "statistical_run"
    assert durable.steps[0].status is TaskStepStatus.READY
    assert context["proposal_message_id"] == task.proposal_message_id
    assert context["draft_hash"] == task.draft_hash == payload["analysis_draft"]["hash"]
    assert service.statistics.create_statistical_design(request, "task-create") == frozen
    with pytest.raises(ConflictError) as duplicate_plan:
        service.statistics.create_statistical_design(request, "task-create-another-key")
    assert duplicate_plan.value.code == "statistics_work_plan_already_created"
    assert duplicate_plan.value.details["plan_revision_id"] == plan_id
    service.statistics.validate_statistical_design(
        plan_id, StatisticalDesignValidationRequest(expected_version=1), "task-validate"
    )
    _approve(service, plan_id)
    run_request = StatisticsRunCreate(
        project_id=request.project_id,
        statistical_design_revision_id=plan_id,
        expected_plan_hash=frozen.plan_revision.plan_hash,
    )
    run = service.runs.create_statistics_run(run_request, "task-run")
    fresh = build_service(service.settings)
    try:
        restored = fresh.conversations.get_conversation(conversation_id)
        assert restored.active_run_id == run.run_id
        assert restored.foreground_agent_role is WorkAgentRole.ANALYSIS
        restored_role_task = work_task(restored, WorkAgentRole.ANALYSIS)
        assert restored_role_task is not None
        assert restored_role_task.run_id == run.run_id
        assert restored_role_task.work_task_id is not None
        restored_work_task = fresh.automation.get_task(restored_role_task.work_task_id)
        assert restored_work_task.status is TaskStatus.WAITING_RUN
        assert restored_work_task.authority_refs.workflow_run_id == run.run_id
        assert restored_work_task.steps[0].status is TaskStepStatus.WAITING
        assert preprocessing_source_run(restored, None) is None
        version = restored.version
        assert fresh.runs.create_statistics_run(run_request, "task-run").run_id == run.run_id
        assert fresh.conversations.get_conversation(conversation_id).version == version
        with pytest.raises(ConflictError) as duplicate:
            fresh.runs.create_statistics_run(run_request, "different-key-same-task")
        assert duplicate.value.code == "statistics_work_run_already_created"
        assert len(fresh.runs.list_runs(project_id=request.project_id)) == 1
        switched = fresh.conversations.select_work_agent(
            conversation_id,
            WorkAgentSelection(agent_role="preprocessing", expected_version=version),
            "task-switch",
        )
        assert switched.active_run_id is None
        assert work_task(switched, WorkAgentRole.ANALYSIS).run_id == run.run_id
        assert (
            work_agent_role_for_turn("继续", requested=None, conversation=switched)
            is WorkAgentRole.PREPROCESSING
        )
        # Replaying a committed request returns its response without stealing
        # the foreground role or rewriting that role's task references.
        assert fresh.runs.create_statistics_run(run_request, "task-run").run_id == run.run_id
        assert (
            fresh.conversations.get_conversation(conversation_id).foreground_agent_role
            is WorkAgentRole.PREPROCESSING
        )
        with pytest.raises(ConflictError) as failure:
            fresh.runs.create_statistics_run(run_request, "new-run-wrong-role")
        assert failure.value.code == "work_agent_confirmation_mismatch"
    finally:
        fresh.close()


@pytest.mark.parametrize("operation", ["validate", "approve", "run"])
def test_changed_analysis_draft_blocks_old_confirmation(service, source_root, work_root, operation):
    conversation_id, request, _ = _proposal(service, source_root, work_root)
    frozen = service.statistics.create_statistical_design(request, "task-create")
    plan_id = frozen.plan_revision.plan_revision_id
    if operation != "validate":
        service.statistics.validate_statistical_design(
            plan_id, StatisticalDesignValidationRequest(expected_version=1), "task-validate"
        )
    if operation == "run":
        _approve(service, plan_id)
    _append(
        service,
        conversation_id,
        {"agent_role": "analysis", "analysis_draft": {"hash": "b" * 64, "draft": {}}},
    )
    with pytest.raises(ConflictError) as failure:
        if operation == "validate":
            service.statistics.validate_statistical_design(
                plan_id, StatisticalDesignValidationRequest(expected_version=1), "stale-validate"
            )
        elif operation == "approve":
            _approve(service, plan_id)
        else:
            service.runs.create_statistics_run(
                StatisticsRunCreate(
                    project_id=request.project_id,
                    statistical_design_revision_id=plan_id,
                    expected_plan_hash=frozen.plan_revision.plan_hash,
                ),
                "stale-run",
            )
    assert failure.value.code == "statistics_work_proposal_stale"
    assert service.runs.list_runs(project_id=request.project_id) == []


def test_work_proposal_rejects_modified_design_and_retains_identical_proposal_binding(
    service, source_root, work_root
):
    conversation_id, request, payload = _proposal(service, source_root, work_root)
    altered = request.model_copy(
        update={"correction": request.correction.model_copy(update={"q_threshold": 0.1})}
    )
    with pytest.raises(ConflictError) as failure:
        service.statistics.create_statistical_design(altered, "task-tampered")
    assert failure.value.code == "statistics_work_proposal_mismatch"
    frozen = service.statistics.create_statistical_design(request, "task-create")
    _append(service, conversation_id, payload)
    task = work_task(
        service.conversations.get_conversation(conversation_id), WorkAgentRole.ANALYSIS
    )
    assert task.plan_revision_id == frozen.plan_revision.plan_revision_id
    assert (
        task.proposal_message_id == frozen.plan_revision.plan["work_context"]["proposal_message_id"]
    )
    assert service.statistics.create_statistical_design(request, "task-create") == frozen


def test_role_selection_is_versioned_idempotent_and_work_only(service):
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work"), "role-work"
    )
    app = create_app(service.settings, service=service)
    with TestClient(app) as client:
        path = f"/api/v1/conversations/{conversation.conversation_id}/work-agent"
        body = {"agent_role": "analysis", "expected_version": conversation.version}
        selected = client.patch(path, json=body, headers={"Idempotency-Key": "select-analysis"})
        assert (
            selected.status_code == 200 and selected.json()["foreground_agent_role"] == "analysis"
        )
        assert (
            client.patch(path, json=body, headers={"Idempotency-Key": "select-analysis"}).json()
            == selected.json()
        )
        assert (
            client.patch(
                path, json=body, headers={"Idempotency-Key": "stale-selection"}
            ).status_code
            == 409
        )
    chat = service.conversations.create_conversation(ConversationCreate(mode="chat"), "role-chat")
    with pytest.raises(InputValidationError) as failure:
        service.conversations.select_work_agent(
            chat.conversation_id,
            WorkAgentSelection(agent_role="analysis", expected_version=1),
            "chat-role",
        )
    assert failure.value.code == "work_agent_mode_required"


def test_preprocessing_and_analysis_runs_survive_switches_and_upstream_references(
    service,
    source_root,
    work_root,
):
    conversation_id, request, payload = _proposal(service, source_root, work_root)
    frozen = service.statistics.create_statistical_design(request, "task-create")
    plan_id = frozen.plan_revision.plan_revision_id
    service.statistics.validate_statistical_design(
        plan_id, StatisticalDesignValidationRequest(expected_version=1), "task-validate"
    )
    _approve(service, plan_id)
    analysis_run = service.runs.create_statistics_run(
        StatisticsRunCreate(
            project_id=request.project_id,
            statistical_design_revision_id=plan_id,
            expected_plan_hash=frozen.plan_revision.plan_hash,
        ),
        "task-run",
    )
    pre_plan = make_approved_plan(service, request.project_id)
    preprocessing_run = service.repository.create_run(
        project_id=request.project_id,
        plan_revision_id=pre_plan.plan_revision_id,
        expected_plan_hash=pre_plan.plan_hash,
        max_attempts=1,
        payload={"executor_type": "workflow_mock", "execution_backend": "mock"},
    )
    _append(
        service,
        conversation_id,
        {"agent_role": "preprocessing", "work_draft": {"hash": "e" * 64}},
        preprocessing_run.run_id,
    )
    _append(service, conversation_id, payload, preprocessing_run.run_id)
    restored = service.conversations.get_conversation(conversation_id)
    assert restored.active_run_id == analysis_run.run_id
    assert work_task(restored, WorkAgentRole.PREPROCESSING).run_id == preprocessing_run.run_id
    assert preprocessing_source_run(restored, None) == preprocessing_run.run_id
    assert work_task(restored, WorkAgentRole.ANALYSIS).plan_revision_id == plan_id
    selected = service.conversations.select_work_agent(
        conversation_id,
        WorkAgentSelection(
            agent_role="preprocessing",
            expected_version=restored.version,
        ),
        "restore-preprocessing",
    )
    assert selected.active_run_id == preprocessing_run.run_id
    assert work_task(selected, WorkAgentRole.ANALYSIS).run_id == analysis_run.run_id


def test_draft_change_between_preparation_and_enqueue_is_rechecked_atomically(
    service,
    source_root,
    work_root,
    monkeypatch,
):
    conversation_id, request, _ = _proposal(service, source_root, work_root)
    frozen = service.statistics.create_statistical_design(request, "task-create")
    plan_id = frozen.plan_revision.plan_revision_id
    service.statistics.validate_statistical_design(
        plan_id, StatisticalDesignValidationRequest(expected_version=1), "task-validate"
    )
    _approve(service, plan_id)
    original = service.statistics._require_plan_current

    def change_draft(plan, *, verify_source_files=True):
        original(plan, verify_source_files=verify_source_files)
        if verify_source_files:
            _append(
                service,
                conversation_id,
                {"agent_role": "analysis", "analysis_draft": {"hash": "f" * 64}},
            )

    monkeypatch.setattr(service.runs, "_require_plan_current", change_draft)
    with pytest.raises(ConflictError) as failure:
        service.runs.create_statistics_run(
            StatisticsRunCreate(
                project_id=request.project_id,
                statistical_design_revision_id=plan_id,
                expected_plan_hash=frozen.plan_revision.plan_hash,
            ),
            "task-race",
        )
    assert failure.value.code == "statistics_work_proposal_stale"
    assert service.runs.list_runs(project_id=request.project_id) == []
    assert service.conversations.get_conversation(conversation_id).active_run_id is None
