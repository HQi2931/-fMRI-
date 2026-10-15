from __future__ import annotations

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
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
from neuroagent.application.contracts.conversations import ConversationCreate
from neuroagent.application.contracts.plans import (
    PlanRevisionCreate,
    SkillPlanIntent,
    SkillPlanResolveRequest,
)
from neuroagent.application.contracts.projects import DatasetCreate, ManifestScanRequest
from neuroagent.application.hashing import content_hash
from neuroagent.domain.fmri import (
    AnalysisImage,
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
    service, source_root: Path, work_root: Path, key: str, *, plan_project_id: str | None = None
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
        covariates=(),
        contrast=(1.0,),
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
def test_step_plan_review_api_returns_exact_read_only_preprocessing_and_statistical_plans(
    service, source_root, work_root, kind
) -> None:
    if kind == "preprocessing":
        project, conversation, plan, task = _preprocessing_review_task(
            service, source_root, work_root, "review-preprocessing"
        )
    else:
        project, conversation, plan, task = _statistical_review_task(
            service, source_root, work_root, "review-statistical"
        )
    step = task.steps[1]
    before = service.automation.get_task(task.task_id)
    service_view = service.automation.get_step_plan_review(
        conversation.conversation_id, task.task_id, step.step_id
    )
    assert service_view.task_version == task.version
    assert service_view.plan_revision_id == plan.plan_revision_id
    assert service_view.execution_authorized is False

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
    assert payload["task_version"] == task.version
    assert payload["task_status"] == "waiting_user"
    assert payload["step_id"] == step.step_id
    assert payload["step_version"] == step.version
    assert payload["step_input_version"] == step.input_version
    assert payload["input_revision"] == task.authority_refs.input_revision
    assert payload["plan_revision_id"] == plan.plan_revision_id
    assert payload["plan_hash"] == plan.plan_hash
    assert payload["plan_state"] == plan.state.value
    assert payload["execution_authorized"] is False
    assert payload["plan"]["plan_revision_id"] == plan.plan_revision_id
    assert payload["plan"]["plan"] == plan.plan
    if kind == "preprocessing":
        assert payload["expected_artifacts"]
    else:
        assert payload["expected_artifacts"] == []
    assert service.automation.get_task(task.task_id) == before
    assert service.plans.get_plan(plan.plan_revision_id).state == plan.state


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
