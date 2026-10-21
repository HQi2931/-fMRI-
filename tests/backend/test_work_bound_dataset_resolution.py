"""Work compilation must recover dataset identity only from an unambiguous project binding."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from neuroagent.application.contracts.conversations import (
    ConversationCreate,
    ConversationTurnCreate,
)
from neuroagent.application.contracts.plans import PlanRevisionCreate
from neuroagent.application.contracts.projects import DatasetCreate, ProjectCreate
from neuroagent.application.conversation_work import WorkActionResult
from tests.backend.test_work_draft import complete_draft


def _register_dataset(service, project_id: str, source_root, *, name: str, key: str):
    from pathlib import Path

    dataset_root = Path(source_root) / name
    dataset_root.mkdir()
    project = service.projects.get_project(project_id)
    return service.projects.create_dataset(
        project_id,
        DatasetCreate(
            name=name,
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        key,
    )


def test_compile_resolves_single_registered_dataset_without_payload_or_new_project(
    service, source_root, work_root, monkeypatch
):
    project = service.projects.create_project(
        ProjectCreate(
            name="Bound project",
            source_roots=[str(source_root)],
            work_root=str(work_root / "bound-project"),
        ),
        "bound-project-create",
    )
    dataset = _register_dataset(
        service, project.project_id, source_root, name="registered-input", key="bound-dataset"
    )
    manifest = service.repository.create_manifest(
        dataset.dataset_id,
        expected_version=dataset.version,
        content={
            "profile": {
                "kind": "dpabi_ready",
                "file_count": 0,
                "nifti_count": 0,
                "dicom_count": 0,
                "subject_count": 0,
                "issues": [],
                "warnings": [],
            },
            "subjects": [],
            "selected_input_stage": "FunRaw",
        },
    )
    plan = service.plans.create_plan(
        PlanRevisionCreate(
            project_id=project.project_id,
            expected_project_version=service.projects.get_project(project.project_id).version,
            plan={"skill_id": "test.mock", "steps": [{"capability": "mock.execute"}]},
            manifest_hash=manifest.content_hash,
            environment_hash="e" * 64,
        ),
        "bound-plan-create",
    )
    conversation = service.conversations.create_conversation(
        ConversationCreate(
            mode="work",
            project_id=project.project_id,
            workspace_path=dataset.source_path,
        ),
        "bound-conversation-create",
    )
    assert all(
        "dataset" not in message.payload and "skill_plan" not in message.payload
        for message in conversation.messages
    )

    compile_request = Mock()
    setup_workspace = Mock(side_effect=AssertionError("bound project must not create a project"))

    def prepare_preprocessing_plan(request, workspace_path, project_id, idempotency_key):
        compile_request.request = request
        compile_request.workspace_path = workspace_path
        compile_request.project_id = project_id
        return WorkActionResult(
            "compiled",
            payload={
                "plan_revision": {
                    "plan_revision_id": plan.plan_revision_id,
                    "plan_hash": plan.plan_hash,
                }
            },
            project_id=project_id,
            workspace_path=workspace_path,
        )

    monkeypatch.setattr(
        service.conversation_work,
        "_prepare_preprocessing_plan",
        prepare_preprocessing_plan,
    )
    monkeypatch.setattr(service.conversation_work, "_setup_workspace", setup_workspace)

    result = service.work_dialogue._compile(
        conversation,
        ConversationTurnCreate(content="确认方案"),
        complete_draft(),
        dataset.source_path,
        project.project_id,
        "compile-bound-project",
    )

    assert "plan_revision" in result.payload
    assert result.project_id == project.project_id
    assert result.payload["confirmation_request"]["options"]
    assert compile_request.request.skill_plan_intent.project_id == project.project_id
    assert compile_request.request.skill_plan_intent.dataset_ref == dataset.dataset_id
    assert compile_request.request.skill_plan_intent.input_manifest_hash == manifest.content_hash
    assert compile_request.workspace_path == dataset.source_path
    assert compile_request.project_id == project.project_id
    assert [item.project_id for item in service.projects.list_projects()] == [project.project_id]
    assert service.repository.list_datasets_for_project(project.project_id) == [
        service.projects.get_dataset(dataset.dataset_id)
    ]
    setup_workspace.assert_not_called()


@pytest.mark.parametrize(
    ("dataset_count", "expected_message"),
    [
        (0, "尚无已登记数据集"),
        (2, "已登记多个数据集"),
    ],
)
def test_compile_blocks_when_project_dataset_is_missing_or_ambiguous(
    service, source_root, work_root, monkeypatch, dataset_count, expected_message
):
    project = service.projects.create_project(
        ProjectCreate(
            name="Bound project",
            source_roots=[str(source_root)],
            work_root=str(work_root / "bound-project"),
        ),
        f"blocked-project-{dataset_count}",
    )
    for index in range(dataset_count):
        _register_dataset(
            service,
            project.project_id,
            source_root,
            name=f"registered-input-{index}",
            key=f"blocked-dataset-{index}",
        )
    conversation = service.conversations.create_conversation(
        ConversationCreate(
            mode="work",
            project_id=project.project_id,
            workspace_path=str(source_root),
        ),
        f"blocked-conversation-{dataset_count}",
    )
    setup_workspace = Mock(side_effect=AssertionError("bound project must not create a project"))
    compile_plan = Mock(side_effect=AssertionError("ambiguous binding must not compile"))
    monkeypatch.setattr(service.conversation_work, "_setup_workspace", setup_workspace)
    monkeypatch.setattr(service.conversation_work, "_prepare_preprocessing_plan", compile_plan)

    result = service.work_dialogue._compile(
        conversation,
        ConversationTurnCreate(content="确认方案"),
        complete_draft(),
        str(source_root),
        project.project_id,
        f"compile-blocked-{dataset_count}",
    )

    assert expected_message in result.assistant_content
    assert "plan_revision" not in result.payload
    assert "confirmation_request" not in result.payload
    assert [item.project_id for item in service.projects.list_projects()] == [project.project_id]
    assert len(service.repository.list_datasets_for_project(project.project_id)) == dataset_count
    setup_workspace.assert_not_called()
    compile_plan.assert_not_called()
