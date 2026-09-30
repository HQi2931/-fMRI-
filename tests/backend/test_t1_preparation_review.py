"""Preparation approvals bind actual bytes and cannot authorize later changed inputs."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from neuroagent.application.contracts import ExecutionBackend, PlanState, WorkflowState
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.application.t1_preparation import (
    T1PreparationMixin,
    T1PreparationReviewCreate,
    T1PreparationRunCreate,
)
from neuroagent.infrastructure.filesystem.t1_review import validate_t1_preparation_artifact


class Reviewer(T1PreparationMixin):
    def _idempotent_prepared(self, **kwargs):
        return kwargs["finalize"](kwargs["prepare"]())


@pytest.fixture
def reviewer(tmp_path: Path):
    service = Reviewer()
    root = tmp_path / "run-1"
    root.mkdir()
    data = bytearray(352 + 2 * 2 * 2 * 4)
    struct.pack_into("<i", data, 0, 348)
    struct.pack_into("<8h", data, 40, 3, 2, 2, 2, 1, 1, 1, 1)
    struct.pack_into("<h", data, 70, 16)
    struct.pack_into("<8f", data, 76, 1, 1, 1, 1, 0, 0, 0, 0)
    paths = {
        "preprocessing.t1_prepared": ("coT1.nii", bytes(data)),
        "preprocessing.t1_review_image": (
            "review.png",
            b"\x89PNG\r\n\x1a\n"
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">II", 100, 100)
            + bytes(9),
        ),
        "preprocessing.metadata_evidence": (
            "evidence.json",
            json.dumps(
                {
                    "operation": "t1_preparation",
                    "requires_human_review": True,
                    "outputs": [
                        {
                            "subject_id": "s1",
                            "relative_path": "coT1.nii",
                            "review_image": "review.png",
                            "metadata": {
                                "dimensions": [2, 2, 2],
                                "volume_count": 1,
                                "affine": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
                            },
                        }
                    ],
                }
            ).encode(),
        ),
    }
    artifacts = []
    for index, (role, (name, payload)) in enumerate(paths.items()):
        (root / name).write_bytes(payload)
        artifacts.append(
            SimpleNamespace(
                artifact_id=f"a{index}",
                artifact_type=role,
                relative_path=name,
                checksum=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                provenance={"subject_id": "s1"},
            )
        )
    events = []
    repo = Mock()
    repo.get_run.return_value = SimpleNamespace(
        run_id="run-1",
        project_id="p1",
        plan_revision_id="plan1",
        version=1,
        state=WorkflowState.SUCCEEDED,
    )
    repo.get_plan.return_value = SimpleNamespace(
        manifest_hash="a" * 64, plan_hash="b" * 64, plan={"dataset_manifest_id": "m1"}
    )
    repo.get_manifest.return_value = SimpleNamespace(
        content_hash="a" * 64, subjects=[SimpleNamespace(subject_id="s1")]
    )
    repo.get_project.return_value = SimpleNamespace(work_root=str(tmp_path))
    repo.list_artifacts.return_value = artifacts
    repo.list_events.side_effect = lambda _run: events
    repo.get_artifact.side_effect = lambda ident: next(
        a for a in artifacts if a.artifact_id == ident
    )
    repo.append_event.side_effect = lambda **kwargs: events.append(
        SimpleNamespace(event_type=kwargs["event_type"], payload=kwargs["payload"])
    )
    service.repository = repo
    service.t1_preparation_artifact_validator = validate_t1_preparation_artifact
    return service


def request(view, **kwargs):
    return T1PreparationReviewCreate(
        expected_run_version=1,
        evidence_hash=view.evidence_hash,
        approved=True,
        orientation_confirmed=True,
        crop_confirmed=True,
        origin_confirmed=True,
        actor="operator",
        reason="checked",
        **kwargs,
    )


def test_review_then_bind_rechecks_bytes(reviewer):
    view = reviewer.get_t1_preparation_review("run-1")
    assert not view.approved
    approved = reviewer.review_t1_preparation("run-1", request(view), "review-key")
    assert approved.review_record_id
    bindings = reviewer.resolve_reviewed_t1_bindings("run-1", "a" * 64, ("s1",))
    assert bindings[0]["artifact_id"] == "a0"
    path, media = reviewer.t1_preparation_preview_path("run-1", "a1")
    assert media == "image/png"
    path.write_bytes(b"changed")
    with pytest.raises(ConflictError, match="产物已变化"):
        reviewer.resolve_reviewed_t1_bindings("run-1", "a" * 64, ("s1",))


def test_review_requires_all_three_visual_checks(reviewer):
    view = reviewer.get_t1_preparation_review("run-1")
    invalid = request(view).model_copy(update={"origin_confirmed": False})
    with pytest.raises(InputValidationError):
        reviewer.review_t1_preparation("run-1", invalid, "review-key")


def test_changed_manifest_or_cohort_cannot_reuse_review(reviewer):
    view = reviewer.get_t1_preparation_review("run-1")
    reviewer.review_t1_preparation("run-1", request(view), "review-key")
    with pytest.raises(ConflictError):
        reviewer.resolve_reviewed_t1_bindings("run-1", "c" * 64, ("s1",))
    with pytest.raises(ConflictError):
        reviewer.resolve_reviewed_t1_bindings("run-1", "a" * 64, ("s2",))


def test_mock_review_cannot_authorize_real_processing(reviewer):
    reviewer.repository.list_artifacts.return_value[0].provenance["non_scientific"] = True
    view = reviewer.get_t1_preparation_review("run-1")
    reviewer.review_t1_preparation("run-1", request(view), "review-key")
    with pytest.raises(ConflictError, match="Mock"):
        reviewer.resolve_reviewed_t1_bindings("run-1", "a" * 64, ("s1",))
    assert reviewer.resolve_reviewed_t1_bindings(
        "run-1", "a" * 64, ("s1",), allow_non_scientific=True
    )


def test_missing_inspection_subject_and_path_escape_block(reviewer):
    artifacts = reviewer.repository.list_artifacts.return_value
    artifacts[1].provenance["subject_id"] = "other"
    with pytest.raises(ConflictError):
        reviewer.get_t1_preparation_review("run-1")
    artifacts[1].relative_path = "../outside.png"
    with pytest.raises(ConflictError):
        reviewer.get_t1_preparation_review("run-1")


def test_headless_review_rejects_invalid_header(reviewer):
    artifact = reviewer.repository.list_artifacts.return_value[0]
    artifact.relative_path = "review.png"
    image = reviewer.repository.list_artifacts.return_value[1]
    artifact.checksum, artifact.size_bytes = image.checksum, image.size_bytes
    with pytest.raises(ConflictError, match="完整性检查"):
        reviewer.get_t1_preparation_review("run-1")


def setup_queue(reviewer):
    plan = reviewer.repository.get_plan.return_value
    plan.state = PlanState.APPROVED
    plan.project_id = "p1"
    plan.environment_hash = "c" * 64
    reviewer.repository.create_run.return_value = reviewer.repository.get_run.return_value
    subject = reviewer.repository.get_manifest.return_value.subjects[0]
    subject.anatomical_files = ["T1.nii"]
    subject.functional_files = ["func.nii"]
    reviewer.repository.get_approved_plan_approval.return_value = SimpleNamespace(
        approval_id="approved1"
    )
    reviewer._require_plan_current = Mock()
    reviewer._validate_execution_backend = Mock()
    return T1PreparationRunCreate(project_id="p1", expected_plan_hash="b" * 64)


def test_preparation_queues_isolated_operation_with_its_own_approval(reviewer):
    body = setup_queue(reviewer)
    reviewer.create_t1_preparation_run("plan1", body, "queue-key")
    payload = reviewer.repository.create_run.call_args.kwargs["payload"]
    assert payload["operation"] == "t1_preparation"
    assert payload["workspace_mode"] == "isolated"
    assert payload["approval_record_id"] == "approved1"
    assert payload["execution_backend"] == "mock"
    assert payload["qc_mode"] == "skip"
    assert reviewer._require_plan_current.call_count == 2


def test_preparation_rejects_unapproved_plan_and_ambiguous_t1(reviewer):
    body = setup_queue(reviewer)
    reviewer.repository.get_plan.return_value.state = PlanState.AWAITING_APPROVAL
    with pytest.raises(ConflictError):
        reviewer.create_t1_preparation_run("plan1", body, "queue-key")
    reviewer.repository.get_plan.return_value.state = PlanState.APPROVED
    reviewer.repository.get_manifest.return_value.subjects[0].anatomical_files.append("other.nii")
    with pytest.raises(InputValidationError):
        reviewer.create_t1_preparation_run("plan1", body, "queue-key")
    reviewer.repository.create_run.assert_not_called()


def test_real_preparation_uses_same_execution_authorization_gate(reviewer):
    body = setup_queue(reviewer).model_copy(update={"execution_backend": ExecutionBackend.MATLAB})
    reviewer._validate_execution_backend.side_effect = ConflictError(
        "approval_required", "confirmation"
    )
    with pytest.raises(ConflictError):
        reviewer.create_t1_preparation_run("plan1", body, "queue-key")
    reviewer.repository.create_run.assert_not_called()


def test_rejection_revokes_latest_review_and_unknown_preview_is_denied(reviewer):
    view = reviewer.get_t1_preparation_review("run-1")
    reviewer.review_t1_preparation("run-1", request(view), "approve-key")
    reviewer.review_t1_preparation(
        "run-1", request(view).model_copy(update={"approved": False}), "reject-key"
    )
    with pytest.raises(ConflictError):
        reviewer.resolve_reviewed_t1_bindings("run-1", "a" * 64, ("s1",))
    with pytest.raises(ConflictError):
        reviewer.t1_preparation_preview_path("run-1", "other-artifact")
    with pytest.raises(InputValidationError):
        reviewer.t1_preparation_preview_path("run-1", "a0")


@pytest.mark.parametrize(
    "mutation", ["dimensions", "singular_affine", "escaped_path", "truncated_voxels"]
)
def test_geometry_evidence_cannot_disagree_with_actual_image(reviewer, mutation):
    path, _ = reviewer.t1_preparation_preview_path("run-1", "a2")
    evidence = json.loads(path.read_text())
    if mutation == "dimensions":
        evidence["outputs"][0]["metadata"]["dimensions"] = [3, 3, 3]
    elif mutation == "singular_affine":
        evidence["outputs"][0]["metadata"]["affine"][0] = [0, 0, 0, 0]
    elif mutation == "escaped_path":
        evidence["outputs"][0]["relative_path"] = "../outside.nii"
    else:
        prepared = path.parent / "coT1.nii"
        prepared.write_bytes(prepared.read_bytes()[:352])
        with pytest.raises(ValueError, match="voxel payload"):
            validate_t1_preparation_artifact(prepared, "preprocessing.t1_prepared")
        return
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError):
        validate_t1_preparation_artifact(path, "preprocessing.metadata_evidence")
