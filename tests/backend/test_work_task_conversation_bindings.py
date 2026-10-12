"""A preprocessing Plan becomes one durable goal across review and Run start."""

# ruff: noqa: RUF001

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from neuroagent.api import create_app
from neuroagent.application.automation.models import TaskStatus
from neuroagent.application.contracts.conversations import ConversationCreate, ConversationMode
from neuroagent.application.contracts.plans import ApprovalCreate, ApprovalDecision
from neuroagent.application.contracts.projects import (
    DatasetCreate,
    ManifestScanRequest,
    ProjectCreate,
)
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.work_draft import draft_hash
from neuroagent.bootstrap import build_service
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
        user_goal = "请对这批静息态 fMRI 数据做预处理，并保留预处理后的目标产物。"
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
                },
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
            assert task.authority_refs.input_revision == plan_data["manifest_hash"]
            assert user_goal in task.goal.description
            assert any("preprocessed_bold" in item for item in task.goal.completion_conditions)

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
                    },
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
            assert current_view.run_id is None
            assert service.automation.get_task(target_task_id).status is TaskStatus.WAITING_USER
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
            assert metric_guard_view.run_id is None

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
        assert len(runs) == 1
        assert runs[0].run_id == run_id
        assert runs[0].plan_revision_id == plan_id

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
    finally:
        service.close()
