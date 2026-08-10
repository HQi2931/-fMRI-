from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from neuroagent.application.contracts import (
    DatasetCreate,
    DatasetKind,
    DatasetSplitCreate,
    DemographicsImportRequest,
    ManifestScanRequest,
    ProjectCreate,
)
from neuroagent.application.errors import ConflictError, InputValidationError, PathPolicyError
from neuroagent.application.hashing import content_hash
from neuroagent.application.services import NeuroAgentService
from neuroagent.infrastructure.filesystem.path_policy import PathPolicy

from .conftest import make_bids_dataset, make_project


def register_and_scan(service: NeuroAgentService, source_root: Path, work_root: Path):
    project = make_project(service, source_root, work_root)
    bids = make_bids_dataset(source_root)
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="BIDS",
            source_path=str(bids),
            expected_project_version=project.version,
        ),
        "dataset-key",
    )
    before = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in bids.rglob("*")
        if path.is_file()
    }
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "inspect-key",
    )
    after = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in bids.rglob("*")
        if path.is_file()
    }
    assert before == after
    return project, service.get_dataset(dataset.dataset_id), manifest


def test_read_only_bids_scan_and_subject_alignment(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    _, _, manifest = register_and_scan(service, source_root, work_root)
    assert manifest.profile.kind is DatasetKind.BIDS
    assert manifest.profile.subject_count == 2
    assert [entry.subject_id for entry in manifest.subjects] == ["sub-01", "sub-02"]
    assert all(entry.functional_files for entry in manifest.subjects)
    assert all(entry.anatomical_files for entry in manifest.subjects)


def test_bids_scan_does_not_classify_fmap_dwi_masks_or_derivatives_as_functional(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    dataset_root = source_root / "role-safe-bids"
    (dataset_root / "dataset_description.json").parent.mkdir(parents=True)
    (dataset_root / "dataset_description.json").write_text(
        '{"Name":"role-safe","BIDSVersion":"1.9.0"}', encoding="utf-8"
    )
    candidates = {
        "sub-01/func/sub-01_task-rest_bold.nii.gz": b"bold",
        "sub-01/func/sub-01_task-rest_desc-brain_mask.nii.gz": b"func-mask",
        "sub-01/fmap/sub-01_dir-ap_epi.nii.gz": b"field-map",
        "sub-01/dwi/sub-01_dwi.nii.gz": b"diffusion",
        "sub-01/anat/sub-01_T1w.nii.gz": b"t1w",
        "sub-01/anat/sub-01_desc-brain_mask.nii.gz": b"anat-mask",
        "sub-02/fmap/sub-02_dir-ap_epi.nii.gz": b"field-map-only-subject",
        "derivatives/pipeline/sub-01/func/sub-01_task-rest_desc-preproc_bold.nii.gz": (
            b"derived-bold"
        ),
    }
    for relative_path, content in candidates.items():
        path = dataset_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    project = make_project(service, source_root, work_root, key="role-safe-project")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Role-safe BIDS",
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        "role-safe-dataset",
    )
    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "role-safe-scan",
    )

    assert [entry.subject_id for entry in manifest.subjects] == ["sub-01", "sub-02"]
    assert manifest.subjects[0].functional_files == ["sub-01/func/sub-01_task-rest_bold.nii.gz"]
    assert manifest.subjects[0].anatomical_files == ["sub-01/anat/sub-01_T1w.nii.gz"]
    assert manifest.subjects[1].functional_files == []
    assert any("6 个 BIDS NIfTI" in warning for warning in manifest.profile.warnings)


def test_plain_nifti_subject_id_sanitization_collision_fails_closed(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    dataset_root = source_root / "colliding-subjects"
    for subject in ("Subject A", "Subject_A"):
        functional = dataset_root / subject / "func" / "rest.nii"
        functional.parent.mkdir(parents=True)
        functional.write_bytes(f"synthetic-{subject}".encode())
    project = make_project(service, source_root, work_root, key="collision-project")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Colliding subject IDs",
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        "collision-dataset",
    )

    with pytest.raises(InputValidationError) as raised:
        service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=dataset.version),
            "collision-scan",
        )

    assert raised.value.code == "subject_identity_collision"
    assert raised.value.details["normalized_subject_id"] == "Subject_A"
    assert raised.value.details["source_subject_ids"] == ["Subject A", "Subject_A"]


def test_dpabi_ready_scan_locks_one_input_stage_and_keeps_checkpoints_inventory_only(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    dataset_root = source_root / "dpabi-working-directory"
    files = {
        "FunRaw/S001/rest.nii": b"raw-functional",
        "T1Raw/S001/t1.nii": b"raw-anatomical",
        "FunImgARW/S001/processed.nii": b"processed-checkpoint",
        "Results/ALFF/ALFFMap_S001.nii": b"derived-result",
    }
    for relative_path, content in files.items():
        path = dataset_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    before = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in dataset_root.rglob("*")
        if path.is_file()
    }
    project = make_project(service, source_root, work_root, key="dpabi-stage-project")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="DPABI stage-safe dataset",
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        "dpabi-stage-dataset",
    )

    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "dpabi-stage-scan",
    )
    after = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in dataset_root.rglob("*")
        if path.is_file()
    }

    assert before == after
    assert manifest.profile.kind is DatasetKind.DPABI_READY
    assert manifest.profile.file_count == 4
    assert [entry.subject_id for entry in manifest.subjects] == ["S001"]
    assert manifest.subjects[0].functional_files == ["FunRaw/S001/rest.nii"]
    assert manifest.subjects[0].anatomical_files == ["T1Raw/S001/t1.nii"]
    assert all(entry.subject_id not in {"FunImgARW", "Results"} for entry in manifest.subjects)
    assert any("2 个其他 DPABI" in warning for warning in manifest.profile.warnings)

    result_path = dataset_root / "Results" / "ALFF" / "ALFFMap_S001.nii"
    result_path.write_bytes(b"changed-derived-result")
    current_dataset = service.get_dataset(dataset.dataset_id)
    changed = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=current_dataset.version),
        "dpabi-stage-result-change",
    )
    assert changed.content_hash != manifest.content_hash


@pytest.mark.parametrize(
    ("relative_paths", "expected_code"),
    [
        (
            ("FunRaw/S001/raw.nii", "FunImg/S001/converted.nii"),
            "dpabi_input_stage_ambiguous",
        ),
        (("FunImgARW/S001/processed.nii",), "dpabi_input_stage_missing"),
        (("Results/ALFF/ALFFMap_S001.nii",), "dpabi_input_stage_missing"),
        (
            (
                "Results/ALFF/ALFFMap_S001.nii",
                "RealignParameter/S001/mean.nii",
            ),
            "dpabi_input_stage_missing",
        ),
        (
            ("FunRaw/S001/raw.nii", "FunImg-ARW/S001/processed.nii"),
            "dpabi_input_stage_unsupported",
        ),
    ],
)
def test_dpabi_ready_ambiguous_or_unsupported_input_stages_fail_closed(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    relative_paths: tuple[str, ...],
    expected_code: str,
) -> None:
    dataset_root = source_root / f"unsafe-dpabi-{expected_code}"
    for relative_path in relative_paths:
        path = dataset_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative_path.encode())
    project = make_project(
        service,
        source_root,
        work_root,
        key=f"{expected_code}-project",
    )
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Unsafe DPABI stages",
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        f"{expected_code}-dataset",
    )

    with pytest.raises(InputValidationError) as raised:
        service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=dataset.version),
            f"{expected_code}-scan",
        )

    assert raised.value.code == expected_code


def test_manifest_hash_covers_bids_sidecars_and_extensionless_dicom(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    project, dataset, first = register_and_scan(service, source_root, work_root)
    del project
    bids = Path(dataset.source_path)
    description = bids / "dataset_description.json"
    description.write_text('{"Name":"synthetic-updated","BIDSVersion":"1.9.0"}', encoding="utf-8")
    current = service.get_dataset(dataset.dataset_id)
    second = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=current.version),
        "inspect-sidecar-change",
    )
    assert second.content_hash != first.content_hash

    dicom_root = source_root / "extensionless-dicom"
    dicom_root.mkdir()
    extensionless = dicom_root / "IM000001"
    extensionless.write_bytes((b"\0" * 128) + b"DICM" + b"synthetic")
    project = make_project(
        service,
        source_root,
        work_root,
        key="extensionless-dicom-project",
    )
    dicom_dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Extensionless DICOM",
            source_path=str(dicom_root),
            expected_project_version=project.version,
        ),
        "extensionless-dicom-dataset",
    )
    dicom_manifest = service.inspect_dataset(
        dicom_dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dicom_dataset.version),
        "extensionless-dicom-scan",
    )
    assert dicom_manifest.profile.kind is DatasetKind.DICOM
    assert dicom_manifest.profile.dicom_count == 1


def test_subject_first_plain_nifti_tree_does_not_collapse_on_modality_directory(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    dataset_root = source_root / "plain-nifti"
    for subject in ("S001", "S002"):
        functional = dataset_root / subject / "func" / "rest.nii"
        functional.parent.mkdir(parents=True)
        functional.write_bytes(f"synthetic-{subject}".encode())
    project = make_project(service, source_root, work_root, key="plain-nifti-project")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Plain NIfTI",
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        "plain-nifti-dataset",
    )

    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "plain-nifti-scan",
    )

    assert [entry.subject_id for entry in manifest.subjects] == ["S001", "S002"]
    assert all(entry.functional_files for entry in manifest.subjects)


def test_plain_nifti_only_promotes_explicit_modalities_and_excludes_masks_and_results(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    dataset_root = source_root / "plain-role-safe"
    files = {
        "S001/func/rest.nii": b"functional",
        "S001/t1/image.nii": b"anatomical",
        "S001/func/brain_mask.nii": b"mask",
        "S001/results/ALFFMap.nii": b"result",
        "S001/unknown/image.nii": b"unknown",
    }
    for relative_path, content in files.items():
        path = dataset_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    project = make_project(service, source_root, work_root, key="plain-role-safe-project")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Plain role-safe NIfTI",
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        "plain-role-safe-dataset",
    )

    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "plain-role-safe-scan",
    )

    assert manifest.profile.kind is DatasetKind.NIFTI
    assert manifest.profile.nifti_count == 5
    assert [entry.subject_id for entry in manifest.subjects] == ["S001"]
    assert manifest.subjects[0].functional_files == ["S001/func/rest.nii"]
    assert manifest.subjects[0].anatomical_files == ["S001/t1/image.nii"]
    assert any("3 个普通 NIfTI" in warning for warning in manifest.profile.warnings)


def test_modality_first_plain_nifti_tree_uses_explicit_subject_layer(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    dataset_root = source_root / "modality-first"
    for subject in ("S001", "S002"):
        functional = dataset_root / "func" / subject / "rest.nii"
        functional.parent.mkdir(parents=True)
        functional.write_bytes(f"synthetic-{subject}".encode())
    project = make_project(service, source_root, work_root, key="modality-first-project")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Modality-first NIfTI",
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        "modality-first-dataset",
    )

    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "modality-first-scan",
    )

    assert [entry.subject_id for entry in manifest.subjects] == ["S001", "S002"]


def test_plain_nifti_without_subject_layer_fails_closed(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    dataset_root = source_root / "ambiguous-nifti"
    functional = dataset_root / "func" / "rest.nii"
    functional.parent.mkdir(parents=True)
    functional.write_bytes(b"synthetic")
    project = make_project(service, source_root, work_root, key="ambiguous-project")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Ambiguous NIfTI",
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        "ambiguous-dataset",
    )

    with pytest.raises(InputValidationError, match="缺少可确定的受试者层级"):
        service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=dataset.version),
            "ambiguous-scan",
        )


def test_bids_subject_level_anatomy_is_inherited_by_session_functional_scan(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    dataset_root = source_root / "session-bids"
    (dataset_root / "dataset_description.json").parent.mkdir(parents=True)
    (dataset_root / "dataset_description.json").write_text(
        '{"Name":"synthetic","BIDSVersion":"1.9.0"}', encoding="utf-8"
    )
    anatomy = dataset_root / "sub-01" / "anat" / "sub-01_T1w.nii.gz"
    functional = dataset_root / "sub-01" / "ses-01" / "func" / "sub-01_ses-01_task-rest_bold.nii.gz"
    anatomy.parent.mkdir(parents=True)
    functional.parent.mkdir(parents=True)
    anatomy.write_bytes(b"synthetic-t1")
    functional.write_bytes(b"synthetic-bold")
    project = make_project(service, source_root, work_root, key="session-bids-project")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Session BIDS",
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        "session-bids-dataset",
    )

    manifest = service.inspect_dataset(
        dataset.dataset_id,
        ManifestScanRequest(expected_dataset_version=dataset.version),
        "session-bids-scan",
    )

    assert len(manifest.subjects) == 1
    entry = manifest.subjects[0]
    assert (entry.subject_id, entry.session_id) == ("sub-01", "ses-01")
    assert entry.functional_files == ["sub-01/ses-01/func/sub-01_ses-01_task-rest_bold.nii.gz"]
    assert entry.anatomical_files == ["sub-01/anat/sub-01_T1w.nii.gz"]


def test_bids_multiple_subject_level_t1_candidates_do_not_implicitly_pair(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    dataset_root = source_root / "ambiguous-session-bids"
    (dataset_root / "dataset_description.json").parent.mkdir(parents=True)
    (dataset_root / "dataset_description.json").write_text(
        '{"Name":"synthetic","BIDSVersion":"1.9.0"}', encoding="utf-8"
    )
    for acquisition in ("a", "b"):
        anatomy = dataset_root / "sub-01" / "anat" / f"sub-01_acq-{acquisition}_T1w.nii.gz"
        anatomy.parent.mkdir(parents=True, exist_ok=True)
        anatomy.write_bytes(f"synthetic-{acquisition}".encode())
    functional = dataset_root / "sub-01" / "ses-01" / "func" / "sub-01_ses-01_task-rest_bold.nii.gz"
    functional.parent.mkdir(parents=True)
    functional.write_bytes(b"synthetic-bold")
    project = make_project(service, source_root, work_root, key="ambiguous-bids-project")
    dataset = service.create_dataset(
        project.project_id,
        DatasetCreate(
            name="Ambiguous session BIDS",
            source_path=str(dataset_root),
            expected_project_version=project.version,
        ),
        "ambiguous-bids-dataset",
    )

    with pytest.raises(InputValidationError, match="唯一的 subject-level T1"):
        service.inspect_dataset(
            dataset.dataset_id,
            ManifestScanRequest(expected_dataset_version=dataset.version),
            "ambiguous-bids-scan",
        )


def test_path_policy_rejects_root_outside_allowlist(
    service: NeuroAgentService, tmp_path: Path, work_root: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(PathPolicyError):
        service.create_project(
            ProjectCreate(
                name="unsafe",
                source_roots=[str(outside)],
                work_root=str(work_root / "project"),
            ),
            "unsafe-project",
        )


@pytest.mark.parametrize("work_relative", (Path("source"), Path("source/work")))
def test_path_policy_rejects_work_root_inside_read_only_source(
    tmp_path: Path, work_relative: Path
) -> None:
    source = tmp_path / "source"
    source.mkdir()

    with pytest.raises(PathPolicyError, match="不能相同或互相包含"):
        PathPolicy([source], tmp_path / work_relative)


def test_path_policy_rejects_read_only_source_inside_work_root(tmp_path: Path) -> None:
    work = tmp_path / "work"
    source = work / "source"
    source.mkdir(parents=True)

    with pytest.raises(PathPolicyError, match="不能相同或互相包含"):
        PathPolicy([source], work)


def test_demographics_alignment_and_deterministic_subject_split(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    _, dataset, _ = register_and_scan(service, source_root, work_root)
    table = source_root / "demographics.csv"
    table.write_text("participant,group,age\nsub-02,B,30\nsub-01,A,20\n", encoding="utf-8")
    demographics = service.import_demographics(
        dataset.dataset_id,
        DemographicsImportRequest(
            source_path=str(table),
            subject_id_column="participant",
            column_mapping={"group": "group", "age": "age"},
            expected_dataset_version=dataset.version,
        ),
        "demographics-key",
    )
    assert demographics.missing_subject_ids == []
    assert demographics.extra_subject_ids == []
    assert service.get_demographics(demographics.demographics_id) == demographics
    dataset = service.get_dataset(dataset.dataset_id)
    split_request = DatasetSplitCreate(
        expected_dataset_version=dataset.version,
        seed=42,
        train_ratio=0.5,
        validation_ratio=0.0,
        test_ratio=0.5,
        stratify_by="group",
        demographics_revision_id=demographics.demographics_id,
    )
    split = service.create_split(dataset.dataset_id, split_request, "split-key")
    repeated = service.create_split(dataset.dataset_id, split_request, "split-key")
    assert repeated.split_id == split.split_id
    assert service.get_split(split.split_id) == split
    assigned = split.train_subject_ids + split.validation_subject_ids + split.test_subject_ids
    assert sorted(assigned) == ["sub-01", "sub-02"]
    assert len(assigned) == len(set(assigned))


def test_demographics_duplicate_subject_is_blocked(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    _, dataset, _ = register_and_scan(service, source_root, work_root)
    table = source_root / "demographics.csv"
    table.write_text("participant,group\nsub-01,A\nsub-01,B\n", encoding="utf-8")
    with pytest.raises(InputValidationError, match="重复"):
        service.import_demographics(
            dataset.dataset_id,
            DemographicsImportRequest(
                source_path=str(table),
                subject_id_column="participant",
                column_mapping={"group": "group"},
                expected_dataset_version=dataset.version,
            ),
            "duplicate-demo",
        )


def test_idempotency_key_reuse_with_changed_payload_is_conflict(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    make_project(service, source_root, work_root, key="same-key")
    with pytest.raises(ConflictError, match="Idempotency-Key"):
        service.create_project(
            ProjectCreate(
                name="different",
                source_roots=[str(source_root)],
                work_root=str(work_root / "other"),
            ),
            "same-key",
        )


def test_idempotent_mutation_rolls_back_if_response_persistence_crashes(
    service: NeuroAgentService,
    source_root: Path,
    work_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = ProjectCreate(
        name="atomic-idempotency",
        source_roots=[str(source_root)],
        work_root=str(work_root / "atomic-idempotency"),
    )
    original = service.repository.complete_idempotent_request
    calls = 0

    def crash_once(
        scope: str,
        key: str,
        request_hash: str,
        owner_token: str,
        response: BaseModel,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated response persistence crash")
        original(scope, key, request_hash, owner_token, response)

    monkeypatch.setattr(service.repository, "complete_idempotent_request", crash_once)
    with pytest.raises(RuntimeError, match="response persistence crash"):
        service.create_project(request, "atomic-idempotency-key")
    assert service.list_projects() == []

    created = service.create_project(request, "atomic-idempotency-key")
    repeated = service.create_project(request, "atomic-idempotency-key")
    assert repeated == created
    assert service.list_projects() == [created]


def test_expired_orphaned_idempotency_reservation_is_reclaimed(
    service: NeuroAgentService, source_root: Path, work_root: Path
) -> None:
    request = ProjectCreate(
        name="recovered-idempotency",
        source_roots=[str(source_root)],
        work_root=str(work_root / "recovered-idempotency"),
    )
    request_hash = content_hash(request.model_dump(mode="json"))
    service.repository.begin_idempotent_request(
        "projects:create",
        "orphaned-key",
        request_hash,
        "00000000-0000-0000-0000-000000000001",
        300,
    )
    with pytest.raises(ConflictError) as active:
        service.repository.begin_idempotent_request(
            "projects:create",
            "orphaned-key",
            request_hash,
            "00000000-0000-0000-0000-000000000002",
            300,
        )
    assert active.value.code == "idempotency_request_in_progress"

    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    with service.database.session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE idempotency_records "
                "SET lease_expires_at = :expired_at WHERE idempotency_key = :key"
            ),
            {"expired_at": expired_at, "key": "orphaned-key"},
        )

    created = service.create_project(request, "orphaned-key")
    repeated = service.create_project(request, "orphaned-key")
    with pytest.raises(ConflictError) as stale_owner:
        service.repository.complete_idempotent_request(
            "projects:create",
            "orphaned-key",
            request_hash,
            "00000000-0000-0000-0000-000000000001",
            request,
        )
    assert stale_owner.value.code == "idempotency_completion_conflict"
    assert repeated == created
    assert service.list_projects() == [created]


def test_idempotency_lease_renewal_requires_current_pending_owner(
    service: NeuroAgentService,
) -> None:
    scope = "test:renew"
    key = "renew-owner-key"
    request_hash = "a" * 64
    owner = "00000000-0000-0000-0000-000000000001"
    service.repository.begin_idempotent_request(scope, key, request_hash, owner, 30)

    assert service.repository.renew_idempotent_request(scope, key, request_hash, owner, 30)
    assert not service.repository.renew_idempotent_request(
        scope,
        key,
        request_hash,
        "00000000-0000-0000-0000-000000000002",
        30,
    )
    service.repository.release_idempotent_request(scope, key, request_hash, owner)
    assert not service.repository.renew_idempotent_request(scope, key, request_hash, owner, 30)
