"""A preprocessing Plan becomes one durable goal across review and Run start."""

# ruff: noqa: RUF001

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.automation.models import TaskStatus, WaitingCondition, WaitingKind
from neuroagent.application.contracts.conversations import ConversationCreate, ConversationMode
from neuroagent.application.contracts.plans import ApprovalCreate, ApprovalDecision
from neuroagent.application.contracts.projects import (
    DatasetCreate,
    ManifestScanRequest,
    ProjectCreate,
)
from neuroagent.application.contracts.runs import QcReviewState, RunCreate
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.hashing import content_hash
from neuroagent.application.work_agents import analysis_dependency_for_request
from neuroagent.application.work_draft import draft_hash
from neuroagent.bootstrap import build_service
from neuroagent.domain.execution import QcExecutionMode
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.domain.lifecycle import WorkflowState
from neuroagent.infrastructure.persistence.models import PlanRevisionRow
from tests.backend.conftest import make_approved_plan
from tests.backend.test_environment import _fake_stack
from tests.backend.test_work_draft import complete_draft


def test_preprocessing_work_task_binds_plan_and_run_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    work = tmp_path / "work"
    (source / "FunRaw" / "sub-01").mkdir(parents=True)
    work.mkdir()
    (source / "FunRaw" / "sub-01" / "rest.nii").write_bytes(
        (348).to_bytes(4, "little") + b"\0" * 344
    )
    settings = _fake_stack(tmp_path / "fake", "preprocessing").model_copy(
        update={
            "database_url": f"sqlite:///{(tmp_path / 'work-task-bindings.sqlite').as_posix()}",
            "allowed_source_roots": [source],
            "allowed_work_root": work,
            "enable_real_execution": True,
        }
    )
    service = build_service(settings)
    try:
        project = service.projects.create_project(
            ProjectCreate(
                name="WorkTask binding",
                source_roots=[str(source)],
                work_root=str(work / "project"),
            ),
            "work-task-binding-project",
        )
        dataset = service.projects.create_dataset(
            project.project_id,
            DatasetCreate(
                name="Input",
                source_path=str(source),
                expected_project_version=project.version,
            ),
            "work-task-binding-dataset",
        )
        manifest = service.projects.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(
                expected_dataset_version=dataset.version,
                selected_input_stage="FunRaw",
            ),
            "work-task-binding-manifest",
        )
        conversation = service.conversations.create_conversation(
            ConversationCreate(
                mode=ConversationMode.WORK,
                project_id=project.project_id,
                workspace_path=str(source),
            ),
            "work-task-binding-conversation",
        )
        user_goal = "先预处理这批静息态 fMRI 数据，然后对 ALFF 做两组统计比较。"
        analysis_intent = analysis_dependency_for_request(
            user_goal, project.project_id, conversation.conversation_id, "work-task-binding-initial"
        )
        assert analysis_intent is not None
        draft = complete_draft()
        service.repository.append_conversation_exchange(
            conversation.conversation_id,
            user_content=user_goal,
            assistant_content="预处理参数草稿已准备好。",
            assistant_payload={
                "agent_role": "preprocessing",
                "dataset": dataset.model_dump(mode="json"),
                "manifest": manifest.model_dump(mode="json"),
                "work_draft": {
                    "draft": draft.model_dump(mode="json"),
                    "hash": draft_hash(draft),
                    "workspace_path": str(source),
                    "compiled": False,
                    "analysis_intent_task_id": analysis_intent["task_id"],
                    "analysis_request_hash": content_hash(user_goal.strip()),
                },
                "analysis_dependency": analysis_intent,
            },
            tool=None,
            workspace_path=str(source),
            preferred_profile_id=None,
            project_id=project.project_id,
            active_run_id=None,
        )

        with TestClient(create_app(service=service)) as client:
            compiled = client.patch(
                f"/api/v1/conversations/{conversation.conversation_id}/work-draft",
                json={
                    "base_hash": draft_hash(draft),
                    "patch": {"preprocessing": {"dummy_scans": 2}},
                },
                headers={"Idempotency-Key": "work-task-binding-compile"},
            )
            assert compiled.status_code == 200, compiled.text
            compiled_body = compiled.json()
            plan_data = compiled_body["assistant_message"]["payload"]["plan_revision"]
            plan_id = plan_data["plan_revision_id"]
            task_view = next(
                item
                for item in compiled_body["conversation"]["work_tasks"]
                if item["agent_role"] == "preprocessing"
            )
            task_id = task_view["work_task_id"]
            assert task_id is not None, compiled_body
            assert task_view["plan_revision_id"] == plan_id
            task = service.automation.get_task(task_id)
            assert task.status is TaskStatus.WAITING_USER
            assert task.authority_refs.conversation_id == conversation.conversation_id
            assert task.authority_refs.plan_revision_id == plan_id
            assert task.authority_refs.analysis_intent_task_id == analysis_intent["task_id"]
            assert task.authority_refs.analysis_request_hash == content_hash(user_goal.strip())
            assert (
                task.steps[0].authority_refs.analysis_intent_task_id == analysis_intent["task_id"]
            )
            assert task.steps[0].authority_refs.analysis_request_hash == content_hash(
                user_goal.strip()
            )
            assert task.authority_refs.input_revision == plan_data["manifest_hash"]
            assert user_goal in task.goal.description
            assert any("preprocessed_bold" in item for item in task.goal.completion_conditions)

            # Reusing the same frozen Plan for a new ordinary draft must clear
            # the pending composite association before any Run has started.
            service.automation.ensure_preprocessing_plan_task(
                conversation_id=conversation.conversation_id,
                project_id=project.project_id,
                plan_revision_id=plan_id,
                goal_description=task.goal.description,
            )
            cleared_task = service.automation.get_task(task_id)
            assert cleared_task.authority_refs.analysis_intent_task_id is None
            assert all(
                step.authority_refs.analysis_intent_task_id is None for step in cleared_task.steps
            )
            service.automation.ensure_preprocessing_plan_task(
                conversation_id=conversation.conversation_id,
                project_id=project.project_id,
                plan_revision_id=plan_id,
                goal_description=task.goal.description,
                analysis_intent_task_id=analysis_intent["task_id"],
                analysis_request_hash=content_hash(user_goal.strip()),
            )
            task = service.automation.get_task(task_id)

            service.plans.approve_plan(
                plan_id,
                ApprovalCreate(
                    expected_version=plan_data["version"],
                    plan_hash=plan_data["plan_hash"],
                    actor="test researcher",
                    decision=ApprovalDecision.APPROVED,
                    reason="reviewed synthetic preprocessing plan",
                ),
                "work-task-binding-approval",
            )
            reviewed = client.post(
                f"/api/v1/conversations/{conversation.conversation_id}/work-review",
                json={
                    "base_hash": compiled_body["assistant_message"]["payload"]["work_draft"]["hash"]
                },
                headers={"Idempotency-Key": "work-task-binding-review"},
            )
            assert reviewed.status_code == 200, reviewed.text
            reviewed_body = reviewed.json()
            assert (
                next(
                    item
                    for item in reviewed_body["conversation"]["work_tasks"]
                    if item["agent_role"] == "preprocessing"
                )["work_task_id"]
                == task_id
            )
            command = reviewed_body["assistant_message"]["payload"]["confirmation_request"][
                "options"
            ][0]["command"]
            confirm_url = f"/api/v1/conversations/{conversation.conversation_id}/work-confirmation"
            confirmation_body = {
                "command": command,
                "expected_draft_hash": reviewed_body["assistant_message"]["payload"]["work_draft"][
                    "hash"
                ],
            }
            invalid_confirmation = client.post(
                confirm_url,
                json={**confirmation_body, "command": "批准并启动 invalid-command"},
                headers={"Idempotency-Key": "work-task-binding-invalid-confirmation"},
            )
            assert invalid_confirmation.status_code == 409
            assert service.runs.list_runs(project_id=project.project_id) == []
            assert service.automation.get_task(task_id).status is TaskStatus.WAITING_USER

            original_bind = service.automation.bind_preprocessing_run

            def fail_binding(**_kwargs):
                raise ConflictError("test_work_task_bind_failure", "Injected binding failure")

            monkeypatch.setattr(service.automation, "bind_preprocessing_run", fail_binding)
            failed = client.post(
                confirm_url,
                json=confirmation_body,
                headers={"Idempotency-Key": "work-task-binding-start"},
            )
            assert failed.status_code == 409, failed.text
            rolled_back_task = service.automation.get_task(task_id)
            assert rolled_back_task.status is TaskStatus.WAITING_USER
            assert rolled_back_task.authority_refs.workflow_run_id is None
            assert service.runs.list_runs(project_id=project.project_id) == []
            rolled_back_conversation = service.conversations.get_conversation(
                conversation.conversation_id
            )
            rolled_back_view = next(
                item
                for item in rolled_back_conversation.work_tasks
                if item.agent_role.value == "preprocessing"
            )
            assert rolled_back_view.work_task_id == task_id
            assert rolled_back_view.run_id is None
            monkeypatch.setattr(service.automation, "bind_preprocessing_run", original_bind)

            started = client.post(
                confirm_url,
                json=confirmation_body,
                headers={"Idempotency-Key": "work-task-binding-start"},
            )
            replay = client.post(
                confirm_url,
                json=confirmation_body,
                headers={"Idempotency-Key": "work-task-binding-start"},
            )
            run_id = started.json()["assistant_message"]["payload"]["run"]["run_id"]
            consumed_draft = started.json()["assistant_message"]["payload"]["work_draft"]
            consumed_patch = client.patch(
                f"/api/v1/conversations/{conversation.conversation_id}/work-draft",
                json={
                    "base_hash": consumed_draft["hash"],
                    "patch": {"preprocessing": {"dummy_scans": 3}},
                },
                headers={"Idempotency-Key": "work-task-binding-edit-after-start"},
            )
            assert consumed_patch.status_code == 409, consumed_patch.text
            assert consumed_patch.json()["error"]["code"] == "work_draft_already_started"
            unchanged = service.conversations.get_conversation(conversation.conversation_id)
            unchanged_view = next(
                item for item in unchanged.work_tasks if item.agent_role.value == "preprocessing"
            )
            assert unchanged_view.work_task_id == task_id
            assert unchanged_view.plan_revision_id == plan_id
            assert unchanged_view.run_id == run_id
            assert service.automation.get_task(task_id).status is TaskStatus.WAITING_RUN
            pending_outbox = (
                service.automation._repository.get_analysis_outbox_for_preprocessing_task(task_id)
            )
            assert pending_outbox is not None
            assert pending_outbox.intent_task_id == analysis_intent["task_id"]
            assert pending_outbox.run_id == run_id
            assert pending_outbox.plan_revision_id == plan_id
            assert pending_outbox.artifact_ids == ()
            with pytest.raises(ConflictError, match="项目"):
                service.automation.ensure_analysis_proposal_outbox(
                    intent=analysis_intent,
                    conversation_id=conversation.conversation_id,
                    project_id="wrong-project",
                    preprocessing_task_id=task_id,
                )

            # A progress lookup for an older Run from another Work conversation
            # must not create a task for that old Plan or replace this current link.
            target_conversation = service.conversations.create_conversation(
                ConversationCreate(
                    mode=ConversationMode.WORK,
                    project_id=project.project_id,
                    workspace_path=str(source),
                ),
                "work-task-binding-target-conversation",
            )
            service.repository.append_conversation_exchange(
                target_conversation.conversation_id,
                user_content="请对这批数据进行常规预处理。",
                assistant_content="预处理参数草稿已准备好。",
                assistant_payload={
                    "agent_role": "preprocessing",
                    "dataset": dataset.model_dump(mode="json"),
                    "manifest": manifest.model_dump(mode="json"),
                    "work_draft": {
                        "draft": draft.model_dump(mode="json"),
                        "hash": draft_hash(draft),
                        "workspace_path": str(source),
                        "compiled": False,
                    },
                    # Simulate the old WorkDialogue history fallback leaking a
                    # prior same-project composite dependency into this turn.
                    "analysis_dependency": analysis_intent,
                },
                tool=None,
                workspace_path=str(source),
                preferred_profile_id=None,
                project_id=project.project_id,
                active_run_id=None,
            )
            target_compiled = client.patch(
                f"/api/v1/conversations/{target_conversation.conversation_id}/work-draft",
                json={
                    "base_hash": draft_hash(draft),
                    "patch": {"preprocessing": {"dummy_scans": 3}},
                },
                headers={"Idempotency-Key": "work-task-binding-target-compile"},
            )
            assert target_compiled.status_code == 200, target_compiled.text
            target_body = target_compiled.json()
            target_plan_id = target_body["assistant_message"]["payload"]["plan_revision"][
                "plan_revision_id"
            ]
            assert target_plan_id != plan_id
            target_task_view = next(
                item
                for item in target_body["conversation"]["work_tasks"]
                if item["agent_role"] == "preprocessing"
            )
            target_task_id = target_task_view["work_task_id"]
            assert target_task_id is not None
            assert service.automation.get_task(target_task_id).status is TaskStatus.WAITING_USER
            ordinary_task = service.automation.get_task(target_task_id)
            assert ordinary_task.authority_refs.analysis_intent_task_id is None
            assert ordinary_task.authority_refs.analysis_request_hash is None
            assert all(
                step.authority_refs.analysis_intent_task_id is None for step in ordinary_task.steps
            )
            assert (
                service.automation._repository.get_analysis_outbox_for_preprocessing_task(
                    target_task_id
                )
                is None
            )
            target_plan_data = target_body["assistant_message"]["payload"]["plan_revision"]
            service.plans.approve_plan(
                target_plan_id,
                ApprovalCreate(
                    expected_version=target_plan_data["version"],
                    plan_hash=target_plan_data["plan_hash"],
                    actor="test researcher",
                    decision=ApprovalDecision.APPROVED,
                    reason="reviewed ordinary preprocessing plan",
                ),
                "work-task-binding-target-approval",
            )
            target_review = client.post(
                f"/api/v1/conversations/{target_conversation.conversation_id}/work-review",
                json={
                    "base_hash": target_body["assistant_message"]["payload"]["work_draft"]["hash"]
                },
                headers={"Idempotency-Key": "work-task-binding-target-review"},
            )
            assert target_review.status_code == 200, target_review.text
            target_command = target_review.json()["assistant_message"]["payload"][
                "confirmation_request"
            ]["options"][0]["command"]
            target_started = client.post(
                f"/api/v1/conversations/{target_conversation.conversation_id}/work-confirmation",
                json={
                    "command": target_command,
                    "expected_draft_hash": target_review.json()["assistant_message"]["payload"][
                        "work_draft"
                    ]["hash"],
                },
                headers={"Idempotency-Key": "work-task-binding-target-start"},
            )
            assert target_started.status_code == 200, target_started.text
            assert service.automation.get_task(target_task_id).authority_refs.workflow_run_id
            assert (
                service.automation._repository.get_analysis_outbox_for_preprocessing_task(
                    target_task_id
                )
                is None
            )
            historical_task_id = service.automation.preprocessing_task_id(
                target_conversation.conversation_id, plan_id
            )
            with pytest.raises(NotFoundError):
                service.automation.get_task(historical_task_id)
            progress = client.post(
                f"/api/v1/conversations/{target_conversation.conversation_id}/turns",
                json={
                    "content": "查看运行进度",
                    "action": "get_progress",
                    "agent_role": "preprocessing",
                    "target_run_id": run_id,
                },
                headers={"Idempotency-Key": "work-task-binding-historical-progress"},
            )
            assert progress.status_code == 201, progress.text
            assert progress.json()["assistant_message"]["payload"]["run"]["run_id"] == run_id
            current = service.conversations.get_conversation(target_conversation.conversation_id)
            current_view = next(
                item for item in current.work_tasks if item.agent_role.value == "preprocessing"
            )
            assert current_view.work_task_id == target_task_id
            assert current_view.plan_revision_id == target_plan_id
            target_run_id = target_started.json()["assistant_message"]["payload"]["run"]["run_id"]
            assert current_view.run_id == target_run_id
            assert service.automation.get_task(target_task_id).status is TaskStatus.WAITING_RUN
            with pytest.raises(NotFoundError):
                service.automation.get_task(historical_task_id)

            # A metric-only SkillPlan must not become the current preprocessing
            # Plan or clear its durable task/run references in the repository.
            with service.repository._write_session() as session:
                source_plan_row = session.get(PlanRevisionRow, target_plan_id)
                assert source_plan_row is not None
                metric_plan_body = json.loads(source_plan_row.plan_json)
                metric_skill_plan = metric_plan_body["skill_plan"]
                assert isinstance(metric_skill_plan.pop("preprocessing_parameters_hash"), str)
                metric_plan_id = str(uuid4())
                session.add(
                    PlanRevisionRow(
                        plan_revision_id=metric_plan_id,
                        project_id=source_plan_row.project_id,
                        revision=source_plan_row.revision + 1000,
                        version=source_plan_row.version,
                        state=source_plan_row.state,
                        plan_hash=source_plan_row.plan_hash,
                        manifest_hash=source_plan_row.manifest_hash,
                        environment_hash=source_plan_row.environment_hash,
                        plan_json=json.dumps(metric_plan_body),
                        validation_issues_json=source_plan_row.validation_issues_json,
                        supersedes_plan_revision_id=source_plan_row.supersedes_plan_revision_id,
                    )
                )
            service.repository.append_conversation_exchange(
                target_conversation.conversation_id,
                user_content="读取已有指标数据。",
                assistant_content="这是指标计算方案。",
                assistant_payload={
                    "agent_role": "preprocessing",
                    "plan_revision": {"plan_revision_id": metric_plan_id},
                },
                tool=None,
                workspace_path=str(source),
                preferred_profile_id=None,
                project_id=project.project_id,
                active_run_id=None,
                work_task_id=None,
            )
            after_metric_plan = service.conversations.get_conversation(
                target_conversation.conversation_id
            )
            metric_guard_view = next(
                item
                for item in after_metric_plan.work_tasks
                if item.agent_role.value == "preprocessing"
            )
            assert metric_guard_view.work_task_id == target_task_id
            assert metric_guard_view.plan_revision_id == target_plan_id
            assert metric_guard_view.run_id == target_run_id

        assert started.status_code == 200, started.text
        assert replay.status_code == 200, replay.text
        assert replay.json() == started.json()
        waiting_task = service.automation.get_task(task_id)
        assert waiting_task.status is TaskStatus.WAITING_RUN
        assert waiting_task.authority_refs.workflow_run_id == run_id
        assert waiting_task.waiting_condition is not None
        assert waiting_task.waiting_condition.reference == run_id
        bound_conversation = service.conversations.get_conversation(conversation.conversation_id)
        bound_view = next(
            item
            for item in bound_conversation.work_tasks
            if item.agent_role.value == "preprocessing"
        )
        assert bound_view.work_task_id == task_id
        assert bound_view.plan_revision_id == plan_id
        assert bound_view.run_id == run_id
        runs = service.runs.list_runs(project_id=project.project_id)
        assert len(runs) == 2
        primary_run = next(item for item in runs if item.run_id == run_id)
        assert primary_run.plan_revision_id == plan_id

        other_project = service.projects.create_project(
            ProjectCreate(
                name="Other project",
                source_roots=[str(source)],
                work_root=str(work / "other-project"),
            ),
            "work-task-binding-other-project",
        )
        other_conversation = service.conversations.create_conversation(
            ConversationCreate(
                mode=ConversationMode.WORK,
                project_id=other_project.project_id,
                workspace_path=str(source),
            ),
            "work-task-binding-other-conversation",
        )
        with pytest.raises(ConflictError, match="其他项目"):
            service.automation.ensure_preprocessing_plan_task(
                conversation_id=other_conversation.conversation_id,
                project_id=project.project_id,
                plan_revision_id=plan_id,
                goal_description="wrong-project attempt",
            )
        wrong_task_id = service.automation.preprocessing_task_id(
            other_conversation.conversation_id, plan_id
        )
        with pytest.raises(NotFoundError):
            service.automation.get_task(wrong_task_id)

        non_skill_plan = make_approved_plan(
            service,
            project.project_id,
            manifest_hash=manifest.content_hash,
        )
        with pytest.raises(ConflictError, match="SkillPlan"):
            service.automation.ensure_preprocessing_plan_task(
                conversation_id=conversation.conversation_id,
                project_id=project.project_id,
                plan_revision_id=non_skill_plan.plan_revision_id,
                goal_description="wrong plan attempt",
            )

            # A successful Run plus the user's explicit QC skip is insufficient
            # until every frozen target Artifact has matching typed lineage.
            frozen_task = service.automation.get_task(task_id)
            assert len(frozen_task.steps[0].expected_artifacts) == 1
            target = frozen_task.steps[0].expected_artifacts[0]
            assert target.artifact_type == "preprocessing.functional_timeseries"
            assert target.relative_path == "output/sub-01/timeseries.nii"
            run_view = service.runs.get_run(run_id)
            successful_run = run_view.model_copy(
                update={
                    "state": WorkflowState.SUCCEEDED,
                    "qc_status": "skipped",
                    "version": run_view.version + 1,
                }
            )
            lineage = ArtifactLineage(
                artifact_id="verified-timeseries",
                kind=ArtifactKind.FUNCTIONAL_TIMESERIES,
                subject_id=target.subject_id,
                metadata_verified=True,
                tr_seconds=2.0,
                volume_count=20,
                metadata_evidence_hash="c" * 64,
                subject_manifest_hash=manifest.content_hash,
                space="MNI152",
                grid_signature="grid-3mm",
                voxel_size_mm=(3.0, 3.0, 3.0),
                mask_artifact_id=None,
                mask_grid_signature=None,
                temporally_filtered=False,
                frequency_band=None,
                spatially_smoothed=False,
                smoothing_fwhm_mm=None,
                scrubbed=False,
                producer_step_hash="b" * 64,
            )
            artifact = SimpleNamespace(
                artifact_id=lineage.artifact_id,
                project_id=project.project_id,
                run_id=run_id,
                artifact_type=target.artifact_type,
                relative_path=target.relative_path,
                checksum=hashlib.sha256(b"timeseries").hexdigest(),
                size_bytes=10,
                provenance={"lineage": lineage.model_dump(mode="json")},
            )
            service.automation._run_repository = SimpleNamespace(
                get_run=lambda _run_id: successful_run
            )
            service.automation._qc_repository = SimpleNamespace(
                get_latest_qc_review_for_run=lambda _run_id: None,
                get_qc_skip_for_run=lambda _run_id: SimpleNamespace(
                    run_id=run_id, qc_skip_id="explicit-qc-skip"
                ),
            )
            service.automation._artifact_repository = SimpleNamespace(
                list_artifacts=lambda _run_id: [artifact],
                get_artifact=lambda artifact_id: (_ for _ in ()).throw(
                    NotFoundError("artifact", artifact_id)
                ),
            )
            service.automation._event_repository = SimpleNamespace(list_events=lambda **_kwargs: [])
            completed = service.automation.reconcile_waiting_run(task_id)
            assert completed.readiness.value == "completed"
            assert completed.task.status is TaskStatus.COMPLETED
            assert completed.task.steps[0].status.value == "succeeded"
            assert completed.task.steps[0].authority_refs.artifact_ids == ("verified-timeseries",)
            assert completed.task.authority_refs.artifact_ids == ("verified-timeseries",)
            ready_outbox = service.automation._repository.get_analysis_outbox(
                str(analysis_intent["task_id"])
            )
            assert ready_outbox.status.value == "ready"
            assert ready_outbox.artifact_ids == ("verified-timeseries",)
            wrong_subject_lineage = lineage.model_copy(update={"subject_id": "sub-99"})
            wrong_subject_artifact = SimpleNamespace(
                **{
                    **vars(artifact),
                    "provenance": {"lineage": wrong_subject_lineage.model_dump(mode="json")},
                }
            )
            with pytest.raises(ValueError, match="lineage differs"):
                service.automation._verify_preprocessing_artifacts(
                    expected=(target,),
                    artifacts=(wrong_subject_artifact,),
                    project_id=project.project_id,
                    run_id=run_id,
                    manifest_hash=manifest.content_hash,
                    alff=None,
                    reho=None,
                )
            with pytest.raises(ValueError, match="identity or integrity"):
                service.automation._verify_preprocessing_artifacts(
                    expected=(target,),
                    artifacts=(
                        SimpleNamespace(**{**vars(artifact), "project_id": "other-project"}),
                    ),
                    project_id=project.project_id,
                    run_id=run_id,
                    manifest_hash=manifest.content_hash,
                    alff=None,
                    reho=None,
                )

            # An old-style dependency wait with no exact target Artifact blocks
            # instead of being treated as a successful preprocessing goal.
            service.automation._run_repository = service.run_repository
            recovered_task = service.automation.ensure_preprocessing_plan_task(
                conversation_id=target_conversation.conversation_id,
                project_id=project.project_id,
                plan_revision_id=plan_id,
                goal_description=user_goal,
            )
            second_run = service.runs.create_run(
                RunCreate(
                    project_id=project.project_id,
                    plan_revision_id=plan_id,
                    expected_plan_hash=plan_data["plan_hash"],
                    qc_mode=QcExecutionMode.SKIP,
                ),
                "work-task-binding-second-run",
            )
            service.automation.bind_preprocessing_run(
                task_id=recovered_task.task_id,
                conversation_id=target_conversation.conversation_id,
                project_id=project.project_id,
                plan_revision_id=plan_id,
                run_id=second_run.run_id,
            )
            recovered_task = service.automation.transition_task(
                recovered_task.task_id,
                expected_version=recovered_task.version + 1,
                target=TaskStatus.WAITING_DEPENDENCY,
                waiting_condition=WaitingCondition(
                    kind=WaitingKind.DEPENDENCY,
                    reference=f"downstream:{second_run.run_id}",
                    reason="legacy downstream dependency wait",
                ),
            )
            successful_second_run = second_run.model_copy(
                update={
                    "state": WorkflowState.SUCCEEDED,
                    "qc_status": "approved",
                    "version": second_run.version + 1,
                }
            )
            service.automation._run_repository = SimpleNamespace(
                get_run=lambda _run_id: successful_second_run
            )
            approved_review = SimpleNamespace(
                run_id=second_run.run_id,
                state=QcReviewState.APPROVED,
                review=SimpleNamespace(review_revision_id="approved-review"),
            )
            service.automation._qc_repository = SimpleNamespace(
                get_latest_qc_review_for_run=lambda _run_id: approved_review,
                get_qc_skip_for_run=lambda _run_id: None,
            )
            service.automation._artifact_repository = SimpleNamespace(
                list_artifacts=lambda _run_id: [],
                get_artifact=lambda artifact_id: (_ for _ in ()).throw(
                    NotFoundError("artifact", artifact_id)
                ),
            )
            blocked = service.automation.reconcile_waiting_run(recovered_task.task_id)
            assert blocked.readiness.value == "blocked"
            assert blocked.task.status is TaskStatus.BLOCKED
            assert blocked.task.steps[0].status.value == "blocked"
            assert "0 matches" in blocked.reason
    finally:
        service.close()
