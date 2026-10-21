from __future__ import annotations

import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from neuroagent.api import create_app
from neuroagent.api.schemas.automation import StepRunStartCreate as ApiStepRunStartCreate
from neuroagent.application.automation.models import (
    AuthorityReferences,
    TaskBudget,
    TaskStatus,
    TaskStep,
    TaskStepStatus,
    WaitingCondition,
    WaitingKind,
    WorkGoal,
    WorkTask,
)
from neuroagent.application.automation.step_lifecycle import step_plan_review_reference
from neuroagent.application.contracts.automation import StepRunStartCreate
from neuroagent.application.contracts.conversations import ConversationCreate
from neuroagent.application.contracts.plans import (
    ApprovalCreate,
    PlanRevisionCreate,
    SkillPlanIntent,
    SkillPlanResolveRequest,
)
from neuroagent.application.contracts.projects import DatasetCreate, ManifestScanRequest
from neuroagent.application.contracts.statistics import StatisticsRunCreate
from neuroagent.application.errors import ConflictError
from neuroagent.application.hashing import content_hash
from neuroagent.domain.fmri import (
    AnalysisImage,
    Centering,
    CovariateColumn,
    CovariateValue,
    MissingValuePolicy,
    StatisticalDesignRevision,
    StatisticalTest,
    Tail,
)

from .conftest import make_project
from .test_science_agent_api import _minimal_preprocessing_payload


def _project_conversation(service, source_root: Path, work_root: Path, key: str):
    project = make_project(service, source_root, work_root, key=f"{key}-project")
    conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id), f"{key}-conversation"
    )
    return project, conversation


def _task(
    *,
    task_id: str,
    conversation_id: str,
    project_id: str,
    plan_revision_id: str,
    plan_hash: str,
    input_revision: str,
    step_kind: str,
    step_input_version: str,
    expected_artifacts=(),
) -> WorkTask:
    prior = TaskStep(
        step_id=f"{task_id}-prior",
        sequence=0,
        kind="preprocessing_run",
        status=TaskStepStatus.SUCCEEDED,
        authority_refs=AuthorityReferences(
            conversation_id=conversation_id, workflow_run_id=f"{task_id}-prior-run"
        ),
    )
    current = TaskStep(
        step_id=f"{task_id}-current",
        sequence=1,
        kind=step_kind,
        status=TaskStepStatus.READY,
        version=3,
        input_version=step_input_version,
        authority_refs=AuthorityReferences(
            conversation_id=conversation_id,
            plan_revision_id=plan_revision_id,
            input_revision=input_revision,
        ),
        expected_artifacts=expected_artifacts,
    )
    return WorkTask(
        task_id=task_id,
        goal=WorkGoal(
            description="Review the next frozen step plan",
            completion_conditions=("the explicitly reviewed workflow is complete",),
        ),
        status=TaskStatus.WAITING_USER,
        waiting_condition=WaitingCondition(
            kind=WaitingKind.USER_DECISION,
            reference=step_plan_review_reference(task_id, current.step_id, plan_revision_id),
            reason="Review the exact plan and explicitly authorize execution.",
        ),
        authority_refs=AuthorityReferences(
            conversation_id=conversation_id,
            plan_revision_id=plan_revision_id,
            input_revision=input_revision,
        ),
        steps=(prior, current),
        budget=TaskBudget(
            max_steps=10,
            max_model_calls=4,
            max_tokens=5000,
            max_elapsed_seconds=1200,
        ),
        version=7,
    )


def _preprocessing_review_task(service, source_root: Path, work_root: Path, key: str):
    project, conversation = _project_conversation(service, source_root, work_root, key)
    dataset_path = source_root / f"{key}-dpabi" / "FunRaw" / "S001"
    dataset_path.mkdir(parents=True)
    nib.save(
        nib.Nifti1Image(np.ones((3, 4, 5), dtype=np.float32), np.eye(4)),
        dataset_path / "rest.nii",
    )
    dataset = service.projects.create_dataset(
        project.project_id,
        DatasetCreate(
            name=f"{key} DPABI input",
            source_path=str(dataset_path.parent.parent),
            expected_project_version=project.version,
        ),
        f"{key}-dataset",
    )
    manifest = service.projects.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(
            expected_dataset_version=dataset.version,
            selected_input_stage="FunRaw",
        ),
        f"{key}-manifest",
    )
    plan = service.skills.resolve_skill_plan(
        SkillPlanResolveRequest(
            request=SkillPlanIntent(
                project_id=project.project_id,
                dataset_ref=dataset.dataset_id,
                input_manifest_hash=manifest.content_hash,
                requested_metrics=(),
                primary_outputs=(),
                request_preprocessing=True,
                preprocessing=_minimal_preprocessing_payload(),
            ),
            expected_project_version=service.projects.get_project(project.project_id).version,
        ),
        f"{key}-skill-plan",
    ).plan_revision
    outputs = service.automation._derive_preprocessing_expectations(plan, project.project_id)
    task = _task(
        task_id=f"{key}-task",
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        plan_revision_id=plan.plan_revision_id,
        plan_hash=plan.plan_hash,
        input_revision=plan.manifest_hash,
        step_kind="preprocessing_run",
        step_input_version=plan.plan_hash,
        expected_artifacts=outputs,
    )
    service.automation.create_task(task)
    return project, conversation, plan, task


def _statistical_review_task(
    service,
    source_root: Path,
    work_root: Path,
    key: str,
    *,
    plan_project_id: str | None = None,
    include_covariate: bool = False,
):
    project, conversation = _project_conversation(service, source_root, work_root, key)
    plan_project = service.projects.get_project(plan_project_id) if plan_project_id else project
    proposal_hash = content_hash(f"{key}-proposal")
    draft_hash = content_hash(f"{key}-draft")
    design = StatisticalDesignRevision(
        revision_id=f"{key}-design",
        test=StatisticalTest.ONE_SAMPLE_T,
        subject_order=("sub-01", "sub-02", "sub-03"),
        images=tuple(
            AnalysisImage(
                subject_id=subject,
                artifact_id=f"{key}-{subject}-metric",
                group=None,
                condition=None,
            )
            for subject in ("sub-01", "sub-02", "sub-03")
        ),
        group_order=(),
        condition_order=(),
        covariates=(
            (
                CovariateColumn(
                    name="age",
                    values=(
                        CovariateValue(subject_id="sub-01", value=32.0),
                        CovariateValue(subject_id="sub-02", value=41.0),
                        CovariateValue(subject_id="sub-03", value=50.0),
                    ),
                    centering=Centering.GRAND_MEAN,
                ),
            )
            if include_covariate
            else ()
        ),
        contrast=(1.0, 0.0) if include_covariate else (1.0,),
        one_sample_baseline=0.0,
        mask_artifact_id=f"{key}-mask",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id=f"{key}-qc",
        qc_review_hash="c" * 64,
    )
    plan = service.plans.create_plan(
        PlanRevisionCreate(
            project_id=plan_project.project_id,
            expected_project_version=plan_project.version,
            plan={
                "kind": "statistical_design",
                "design": design.model_dump(mode="json"),
                "work_context": {
                    "conversation_id": conversation.conversation_id,
                    "proposal_hash": proposal_hash,
                    "draft_hash": draft_hash,
                },
            },
            manifest_hash="a" * 64,
            environment_hash="b" * 64,
        ),
        f"{key}-plan-create",
    )
    task = _task(
        task_id=f"{key}-task",
        conversation_id=conversation.conversation_id,
        project_id=project.project_id,
        plan_revision_id=plan.plan_revision_id,
        plan_hash=plan.plan_hash,
        input_revision=draft_hash,
        step_kind="statistical_run",
        step_input_version=proposal_hash,
    )
    service.automation.create_task(task)
    return project, conversation, plan, task


@pytest.mark.parametrize("kind", ["preprocessing", "statistical"])
def test_step_plan_review_api_returns_allow_list_details_without_writer_lock(
    service, source_root, work_root, kind, monkeypatch
) -> None:
    if kind == "preprocessing":
        project, conversation, plan, task = _preprocessing_review_task(
            service, source_root, work_root, "review-preprocessing"
        )
    else:
        project, conversation, plan, task = _statistical_review_task(
            service,
            source_root,
            work_root,
            "review-statistical",
            include_covariate=True,
        )
    step = task.steps[1]
    before = service.automation.get_task(task.task_id)

    def reject_write_unit_of_work():
        raise AssertionError("step review reads must not open a write unit of work")

    monkeypatch.setattr(service.automation, "atomic", reject_write_unit_of_work)
    read_review = service.automation._read_step_plan_review

    def verify_shared_read_snapshot(conversation_id: str, task_id: str, step_id: str):
        repositories = (
            service.automation._repository,
            service.automation._conversation_repository,
            service.automation._project_repository,
            service.automation._plan_repository,
        )
        sessions = tuple(repository.unit_of_work.current_session() for repository in repositories)
        assert sessions[0] is not None
        assert all(session is sessions[0] for session in sessions)
        return read_review(conversation_id, task_id, step_id)

    monkeypatch.setattr(service.automation, "_read_step_plan_review", verify_shared_read_snapshot)
    service_view = service.automation.get_step_plan_review(
        conversation.conversation_id, task.task_id, step.step_id
    )
    assert service_view.task_id == task.task_id
    assert service_view.task_version == task.version
    assert service_view.step_version == step.version
    assert service_view.project_id == project.project_id
    assert service_view.step_sequence == step.sequence
    assert service_view.plan_revision_id == plan.plan_revision_id
    assert service_view.plan_revision == plan.revision
    assert service_view.plan_version == plan.version
    assert service_view.plan_hash == plan.plan_hash

    with TestClient(create_app(service=service)) as client:
        response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/work-tasks/"
            f"{task.task_id}/steps/{step.step_id}/review"
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["conversation_id"] == conversation.conversation_id
    assert payload["project_id"] == project.project_id
    assert payload["task_id"] == task.task_id
    assert payload["step_id"] == step.step_id
    assert payload["step_sequence"] == step.sequence
    assert payload["plan_revision_id"] == plan.plan_revision_id
    assert payload["plan_revision"] == plan.revision
    assert payload["plan_hash"] == plan.plan_hash
    assert set(payload) == {
        "conversation_id",
        "project_id",
        "task_id",
        "task_version",
        "step_id",
        "step_sequence",
        "step_version",
        "plan_revision_id",
        "plan_revision",
        "plan_version",
        "plan_hash",
        "images",
        "covariates",
    }
    if kind == "preprocessing":
        assert payload["images"] == []
        assert payload["covariates"] == []
    else:
        assert payload["images"] == [
            {"subject_id": "sub-01", "group": None, "condition": None},
            {"subject_id": "sub-02", "group": None, "condition": None},
            {"subject_id": "sub-03", "group": None, "condition": None},
        ]
        assert payload["covariates"] == [
            {
                "name": "age",
                "centering": "grand_mean",
                "values": [
                    {"subject_id": "sub-01", "value": 32.0},
                    {"subject_id": "sub-02", "value": 41.0},
                    {"subject_id": "sub-03", "value": 50.0},
                ],
            }
        ]
    assert "artifact_id" not in str(payload)
    assert 'plan"' not in str(payload)
    assert "manifest_hash" not in payload
    assert "subject_order" not in str(payload)
    assert service.automation.get_task(task.task_id) == before
    assert service.plans.get_plan(plan.plan_revision_id).state == plan.state


def test_step_plan_review_list_is_scoped_verified_and_refreshed(
    service, source_root, work_root, monkeypatch
) -> None:
    project, conversation, _plan, task = _statistical_review_task(
        service, source_root, work_root, "review-list"
    )
    step = task.steps[1]
    other_conversation = service.conversations.create_conversation(
        ConversationCreate(mode="work", project_id=project.project_id),
        "review-list-other-conversation",
    )

    with TestClient(create_app(service=service)) as client:
        direct = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/work-tasks/"
            f"{task.task_id}/steps/{step.step_id}/review"
        )

    def reject_write_unit_of_work():
        raise AssertionError("list_step_plan_reviews must not open a write unit of work")

    monkeypatch.setattr(service.automation, "atomic", reject_write_unit_of_work)
    with TestClient(create_app(service=service)) as client:
        listed = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/step-plan-reviews"
        )
        wrong_conversation = client.get(
            f"/api/v1/conversations/{other_conversation.conversation_id}/step-plan-reviews"
        )

    assert listed.status_code == 200, listed.text
    assert listed.json()["truncated"] is False
    assert len(listed.json()["items"]) == 1
    item = listed.json()["items"][0]
    assert item["conversation_id"] == conversation.conversation_id
    assert item["project_id"] == project.project_id
    assert item["task_id"] == task.task_id
    assert item["step_id"] == step.step_id
    assert item["step_sequence"] == step.sequence
    assert item["step_kind"] == "statistical_run"
    assert item["plan_revision_id"] == direct.json()["plan_revision_id"]
    assert item["plan_revision"] == direct.json()["plan_revision"]
    assert item["plan_hash"] == direct.json()["plan_hash"]
    assert item["expected_output_count"] == 0
    assert item["execution_authorized"] is False
    assert item["preprocessing"] is None
    assert item["statistical"] == {
        "test": "one_sample_t",
        "group_count": 0,
        "condition_count": 0,
        "image_count": 3,
        "covariate_count": 0,
        "contrast": [1.0],
        "tail": "two_sided",
        "missing_value_policy": "error",
    }
    assert set(item) == {
        "conversation_id",
        "project_id",
        "task_id",
        "task_version",
        "step_id",
        "step_sequence",
        "step_version",
        "step_kind",
        "step_status",
        "plan_revision_id",
        "plan_revision",
        "plan_version",
        "plan_hash",
        "plan_state",
        "execution_authorized",
        "expected_output_count",
        "preprocessing",
        "statistical",
    }
    serialized_item = str(item)
    assert "sub-01" not in serialized_item
    assert f"{task.task_id}-sub-01-metric" not in serialized_item
    assert "subject_order" not in serialized_item
    assert "plan" not in item
    assert wrong_conversation.status_code == 200, wrong_conversation.text
    assert wrong_conversation.json() == {"items": [], "truncated": False}

    current = service.automation.get_task(task.task_id)
    service.automation._repository.save_task(
        current.model_copy(
            update={
                "waiting_condition": current.waiting_condition.model_copy(
                    update={"reference": "stale-step-plan-review"}
                ),
                "version": current.version + 1,
            }
        ),
        expected_version=current.version,
    )
    with TestClient(create_app(service=service)) as client:
        refreshed = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/step-plan-reviews"
        )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json() == {"items": [], "truncated": False}


def test_step_plan_review_candidates_ignore_malformed_persisted_json(
    service, source_root, work_root
) -> None:
    _, conversation, _plan, task = _statistical_review_task(
        service, source_root, work_root, "review-malformed"
    )
    repository = service.automation._repository
    with repository.database.session_factory() as session:
        session.execute(
            text("UPDATE work_tasks SET authority_refs_json=:invalid WHERE task_id=:task_id"),
            {"invalid": "{not-json", "task_id": task.task_id},
        )
        session.commit()

    with TestClient(create_app(service=service)) as client:
        response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/step-plan-reviews"
        )
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "truncated": False}


def test_step_plan_review_list_projects_only_preprocessing_parameters(
    service, source_root, work_root
) -> None:
    project, conversation, plan, task = _preprocessing_review_task(
        service, source_root, work_root, "review-list-preprocessing"
    )
    with TestClient(create_app(service=service)) as client:
        response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/step-plan-reviews"
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["truncated"] is False
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["project_id"] == project.project_id
    assert item["plan_revision_id"] == plan.plan_revision_id
    assert item["expected_output_count"] == len(task.steps[1].expected_artifacts)
    assert item["preprocessing"] == {
        "tr_seconds": 2.0,
        "expected_time_points": 120,
        "dummy_scans": 0,
        "detrend": False,
        "slice_timing_enabled": False,
        "slice_count": None,
        "reference_slice": None,
        "realignment_enabled": False,
        "head_motion_model": None,
        "white_matter_regression_enabled": False,
        "csf_regression_enabled": False,
        "global_signal_regression_enabled": False,
        "normalization_mode": 0,
        "requested_metrics": [],
        "frequency_low_hz": None,
        "frequency_high_hz": None,
        "mask_source": None,
        "requested_scalings": [],
        "metric_smoothing": False,
    }
    assert item["statistical"] is None
    assert "dataset_ref" not in str(item)
    assert "structural_artifact_id" not in str(item)
    assert plan.plan["skill_plan"]["input_artifact_id"] not in str(item)


def test_exact_step_plan_approval_and_mock_run_authorization_are_bound_atomically(
    service, source_root, work_root
) -> None:
    project, conversation, plan, task = _preprocessing_review_task(
        service, source_root, work_root, "step-action-flow"
    )
    step = task.steps[1]
    root = (
        f"/api/v1/conversations/{conversation.conversation_id}/work-tasks/"
        f"{task.task_id}/steps/{step.step_id}"
    )
    identity = {
        "task_version": task.version,
        "step_version": step.version,
        "plan_revision_id": plan.plan_revision_id,
        "plan_version": plan.version,
        "plan_hash": plan.plan_hash,
    }

    with TestClient(create_app(service=service)) as client:
        stale_approval = client.post(
            f"{root}/approve",
            json={**identity, "task_version": task.version + 1, "actor": "用户", "reason": "stale"},
            headers={"Idempotency-Key": "step-approve-stale"},
        )
        assert stale_approval.status_code == 409, stale_approval.text
        assert stale_approval.json()["error"]["code"] == "step_plan_review_identity_stale"
        assert service.plans.get_plan(plan.plan_revision_id).state.value == "awaiting_approval"

        approved = client.post(
            f"{root}/approve",
            json={**identity, "actor": "用户", "reason": "用户审查当前步骤冻结方案"},
            headers={"Idempotency-Key": "step-approve-valid"},
        )
        assert approved.status_code == 201, approved.text
        approved_payload = approved.json()
        assert approved_payload["conversation_id"] == conversation.conversation_id
        assert approved_payload["project_id"] == project.project_id
        assert approved_payload["task_id"] == task.task_id
        assert approved_payload["step_id"] == step.step_id
        assert approved_payload["plan_hash"] == plan.plan_hash
        current_plan = service.plans.get_plan(plan.plan_revision_id)
        assert current_plan.state.value == "approved"
        assert current_plan.plan_hash == plan.plan_hash

        current_identity = {**identity, "plan_version": current_plan.version}
        issued = client.post(
            f"{root}/run-authorizations",
            json={
                **current_identity,
                "execution_backend": "mock",
                "workspace_mode": "isolated",
                "real_execution_confirmed": False,
                "qc_mode": "require",
            },
        )
        assert issued.status_code == 201, issued.text
        authorization = issued.json()
        assert authorization["conversation_id"] == conversation.conversation_id
        assert authorization["project_id"] == project.project_id
        assert authorization["task_id"] == task.task_id
        assert authorization["step_id"] == step.step_id
        assert authorization["plan_revision_id"] == plan.plan_revision_id
        assert authorization["plan_hash"] == plan.plan_hash

        start_body = {
            **current_identity,
            "confirmation_id": authorization["confirmation_id"],
            "execution_backend": "mock",
            "workspace_mode": "isolated",
            "real_execution_confirmed": False,
            "qc_mode": "require",
            "max_attempts": 1,
            "mock_outcome": "succeed",
            "mock_delay_ms": 0,
        }
        started = client.post(
            f"{root}/run",
            json=start_body,
            headers={"Idempotency-Key": "step-run-start-valid"},
        )
        assert started.status_code == 202, started.text
        started_payload = started.json()
        run_id = started_payload["run"]["run_id"]
        assert started_payload["conversation_id"] == conversation.conversation_id
        assert started_payload["project_id"] == project.project_id
        assert started_payload["task_id"] == task.task_id
        assert started_payload["step_id"] == step.step_id
        assert started_payload["run"]["project_id"] == project.project_id
        assert started_payload["run"]["plan_revision_id"] == plan.plan_revision_id
        replayed = client.post(
            f"{root}/run",
            json=start_body,
            headers={"Idempotency-Key": "step-run-start-valid"},
        )
        assert replayed.status_code == 202, replayed.text
        assert replayed.json() == started_payload
        assert len(service.runs.list_runs(project_id=project.project_id)) == 1

        persisted = service.automation.get_task(task.task_id)
        bound_step = next(item for item in persisted.steps if item.step_id == step.step_id)
        assert persisted.status is TaskStatus.WAITING_RUN
        assert persisted.authority_refs.workflow_run_id == run_id
        assert bound_step.authority_refs.workflow_run_id == run_id
        assert bound_step.status is TaskStepStatus.WAITING
        with service.database.session_factory() as session:
            job = session.execute(
                text("SELECT executor_type, payload_json FROM jobs WHERE run_id=:run_id"),
                {"run_id": run_id},
            ).one()
            challenge = session.execute(
                text(
                    "SELECT conversation_id, project_id, plan_revision_id, plan_hash, qc_mode, "
                    "task_id, step_id, task_version, step_version, plan_version, "
                    "execution_backend, workspace_mode, consumed_run_id "
                    "FROM confirmation_challenges WHERE confirmation_id=:confirmation_id"
                ),
                {"confirmation_id": authorization["confirmation_id"]},
            ).one()
        assert job.executor_type == "workflow_mock"
        job_envelope = json.loads(job.payload_json)
        assert job_envelope["kind"] == "workflow.mock"
        assert job_envelope["command"]["outcome"] == "succeed"
        assert job_envelope["command"]["delay_ms"] == 0
        assert tuple(challenge) == (
            conversation.conversation_id,
            project.project_id,
            plan.plan_revision_id,
            plan.plan_hash,
            "require",
            task.task_id,
            step.step_id,
            task.version,
            step.version,
            current_plan.version,
            "mock",
            "isolated",
            run_id,
        )


def test_step_run_authorization_rejects_stale_step_identity(
    service, source_root, work_root
) -> None:
    _project, conversation, plan, task = _preprocessing_review_task(
        service, source_root, work_root, "step-action-stale"
    )
    step = task.steps[1]
    service.plans.approve_plan(
        plan.plan_revision_id,
        ApprovalCreate(
            expected_version=plan.version,
            plan_hash=plan.plan_hash,
            actor="test",
            decision="approved",
            reason="test fixture approval",
        ),
        "step-action-stale-plan-approval",
    )
    approved_plan = service.plans.get_plan(plan.plan_revision_id)
    with TestClient(create_app(service=service)) as client:
        response = client.post(
            f"/api/v1/conversations/{conversation.conversation_id}/work-tasks/"
            f"{task.task_id}/steps/{step.step_id}/run-authorizations",
            json={
                "task_version": task.version,
                "step_version": step.version + 1,
                "plan_revision_id": plan.plan_revision_id,
                "plan_version": approved_plan.version,
                "plan_hash": plan.plan_hash,
                "execution_backend": "mock",
                "workspace_mode": "isolated",
                "qc_mode": "require",
            },
        )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "step_plan_review_identity_stale"
    with service.database.session_factory() as session:
        count = session.execute(
            text("SELECT COUNT(*) FROM confirmation_challenges WHERE task_id=:task_id"),
            {"task_id": task.task_id},
        ).scalar_one()
    assert count == 0


def test_statistical_run_target_cannot_be_redirected_to_a_different_task_step(
    service, source_root, work_root
) -> None:
    project, _conversation, plan, task = _statistical_review_task(
        service, source_root, work_root, "step-statistical-target"
    )
    active_step = task.steps[1]
    prior_step = task.steps[0]
    with pytest.raises(ConflictError) as failure:
        service.runs.create_statistics_run(
            StatisticsRunCreate(
                project_id=project.project_id,
                statistical_design_revision_id=plan.plan_revision_id,
                expected_plan_hash=plan.plan_hash,
                work_task_id=task.task_id,
                work_step_id=prior_step.step_id,
            ),
            "step-statistical-wrong-target",
        )
    assert failure.value.code == "step_plan_review_step_not_current"
    assert service.automation.get_task(task.task_id) == task
    assert service.runs.list_runs(project_id=project.project_id) == []
    assert active_step.status is TaskStepStatus.READY


def test_matlab_start_contract_rejects_mock_only_controls() -> None:
    shared = {
        "task_version": 1,
        "step_version": 1,
        "plan_revision_id": "plan-1",
        "plan_version": 1,
        "plan_hash": "a" * 64,
        "confirmation_id": "confirmation-1",
        "execution_backend": "matlab",
        "workspace_mode": "isolated",
        "real_execution_confirmed": True,
        "mock_outcome": "fail_terminal",
        "mock_delay_ms": 0,
    }
    with pytest.raises(ValidationError, match="mock run options"):
        ApiStepRunStartCreate.model_validate(shared)
    with pytest.raises(ValidationError, match="mock run options"):
        StepRunStartCreate.model_validate(shared)


def test_step_plan_review_list_truncates_after_validating_at_most_100_candidates(
    service, source_root, work_root
) -> None:
    project, conversation, plan, base_task = _statistical_review_task(
        service, source_root, work_root, "review-truncate"
    )
    for index in range(100):
        task = _task(
            task_id=f"review-truncate-{index:03d}",
            conversation_id=conversation.conversation_id,
            project_id=project.project_id,
            plan_revision_id=plan.plan_revision_id,
            plan_hash=plan.plan_hash,
            input_revision=base_task.authority_refs.input_revision,
            step_kind="statistical_run",
            step_input_version=base_task.steps[1].input_version,
        )
        service.automation.create_task(task)

    candidate_ids = service.automation._repository.list_step_plan_review_candidate_task_ids(
        conversation.conversation_id, limit=101
    )
    assert len(candidate_ids) == 101
    with TestClient(create_app(service=service)) as client:
        response = client.get(
            f"/api/v1/conversations/{conversation.conversation_id}/step-plan-reviews"
        )

    assert response.status_code == 200, response.text
    assert response.json()["truncated"] is True
    assert len(response.json()["items"]) == 100


@pytest.mark.parametrize(
    ("invalid_case", "expected_status"),
    [
        ("wrong_conversation", 404),
        ("wrong_project", 409),
        ("wrong_step", 409),
        ("wrong_plan", 409),
        ("wrong_wait", 409),
        ("multiple_ready", 409),
    ],
)
def test_step_plan_review_api_rejects_unscoped_or_stale_identity(
    service, source_root, work_root, invalid_case, expected_status
) -> None:
    project, conversation, _plan, task = _statistical_review_task(
        service,
        source_root,
        work_root,
        f"review-invalid-{invalid_case}",
        plan_project_id=None,
    )
    step = task.steps[1]
    route_conversation_id = conversation.conversation_id
    route_step_id = step.step_id

    if invalid_case == "wrong_conversation":
        other = service.conversations.create_conversation(
            ConversationCreate(mode="work", project_id=project.project_id),
            f"review-other-conversation-{invalid_case}",
        )
        route_conversation_id = other.conversation_id
    elif invalid_case == "wrong_project":
        other_project = make_project(
            service, source_root, work_root, key=f"{invalid_case}-other-project"
        )
        # Rebind the Plan reference to a valid Plan stored under another
        # project, while retaining the original conversation and Task.
        _, _, other_plan, _ = _statistical_review_task(
            service,
            source_root,
            work_root,
            f"{invalid_case}-foreign-plan",
            plan_project_id=other_project.project_id,
        )
        foreign = service.plans.get_plan(other_plan.plan_revision_id)
        current = service.automation.get_task(task.task_id)
        current_step = current.steps[1].model_copy(
            update={
                "authority_refs": current.steps[1].authority_refs.model_copy(
                    update={"plan_revision_id": foreign.plan_revision_id}
                )
            }
        )
        current_root_refs = current.authority_refs.model_copy(
            update={"plan_revision_id": foreign.plan_revision_id}
        )
        service.automation._repository.save_task(
            current.model_copy(
                update={
                    "steps": (current.steps[0], current_step),
                    "authority_refs": current_root_refs,
                    "waiting_condition": current.waiting_condition.model_copy(
                        update={
                            "reference": step_plan_review_reference(
                                task.task_id, step.step_id, foreign.plan_revision_id
                            )
                        }
                    ),
                    "version": current.version + 1,
                }
            ),
            expected_version=current.version,
        )
    elif invalid_case == "wrong_step":
        route_step_id = "not-the-current-step"
    elif invalid_case == "wrong_plan":
        current = service.automation.get_task(task.task_id)
        current_step = current.steps[1].model_copy(
            update={
                "authority_refs": current.steps[1].authority_refs.model_copy(
                    update={"plan_revision_id": "different-plan-revision"}
                )
            }
        )
        service.automation._repository.save_task(
            current.model_copy(
                update={"steps": (current.steps[0], current_step), "version": current.version + 1}
            ),
            expected_version=current.version,
        )
    elif invalid_case == "wrong_wait":
        current = service.automation.get_task(task.task_id)
        service.automation._repository.save_task(
            current.model_copy(
                update={
                    "waiting_condition": current.waiting_condition.model_copy(
                        update={"reference": "step-plan:stale-reference"}
                    ),
                    "version": current.version + 1,
                }
            ),
            expected_version=current.version,
        )
    elif invalid_case == "multiple_ready":
        current = service.automation.get_task(task.task_id)
        extra = TaskStep(
            step_id=f"{task.task_id}-extra",
            sequence=2,
            kind="statistical_run",
            status=TaskStepStatus.READY,
            input_version=content_hash("another-proposal"),
            authority_refs=AuthorityReferences(conversation_id=conversation.conversation_id),
        )
        service.automation._repository.save_task(
            current.model_copy(
                update={
                    "steps": (*current.steps, extra),
                    "version": current.version + 1,
                }
            ),
            expected_version=current.version,
        )

    with TestClient(create_app(service=service)) as client:
        response = client.get(
            f"/api/v1/conversations/{route_conversation_id}/work-tasks/"
            f"{task.task_id}/steps/{route_step_id}/review"
        )

    assert response.status_code == expected_status, response.text
