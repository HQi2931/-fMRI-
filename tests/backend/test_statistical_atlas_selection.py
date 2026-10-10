from __future__ import annotations

import json
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pytest
from scipy.io import savemat

from neuroagent.application.contracts import (
    ApprovalCreate,
    StatisticalDesignValidationRequest,
    StatisticsRunCreate,
)
from neuroagent.application.errors import ConflictError, InputValidationError
from neuroagent.domain.fmri.results import (
    ClusterRecord,
    RegisteredArtifactMetadata,
    StatisticalArtifactRole,
    StatisticalResultManifest,
)

from .test_work_task_bindings import _proposal


def _atlases(service):
    dpabi = service.environment_provider.configuration().dpabi_dir
    assert dpabi is not None
    root = dpabi / "Templates"
    root.mkdir(parents=True, exist_ok=True)
    for name, labels in (
        ("aal.nii", "aal_Labels.mat"),
        ("AAL3v1_1mm.nii", "AAL3v1_1mm_Labels.mat"),
        ("Brodmann_YCG.nii", "Brodmann_YCG_Labels.mat"),
    ):
        nib.save(nib.Nifti1Image(np.ones((3, 4, 5), dtype=np.int16), np.eye(4)), root / name)
        savemat(
            root / labels,
            {"Reference": np.array([["Region one", 1], ["Region two", 2]], dtype=object)},
        )
    return root


@pytest.mark.parametrize("atlas_id", ["aal", "aal3", "brodmann"])
@pytest.mark.parametrize("stage", ["validation", "approval", "execution"])
def test_atlas_frozen_in_formal_plan_and_changed_bytes_block_approval(
    service, source_root, work_root, atlas_id, stage
):
    root = _atlases(service)
    _, request, _ = _proposal(service, source_root, work_root)
    request = request.model_copy(update={"atlas_id": atlas_id})
    created = service.create_statistical_design(request, "atlas-create")
    selection = created.localization_plan
    assert selection is not None and selection.atlas_id == atlas_id
    assert selection.coordinate_space == "MNI" and selection.cluster_connectivity == 26
    assert created.plan_revision.plan["localization_plan"] == selection.model_dump(mode="json")
    assert (
        service.get_statistical_design(created.plan_revision.plan_revision_id).localization_plan
        == selection
    )
    plan = created.plan_revision
    if stage != "validation":
        plan = service.validate_statistical_design(
            plan.plan_revision_id,
            StatisticalDesignValidationRequest(expected_version=plan.version),
            "atlas-validate",
        ).plan_revision
    approval = ApprovalCreate(
        expected_version=plan.version,
        plan_hash=plan.plan_hash,
        actor="test",
        decision="approved",
        reason="Synthetic atlas mutation fixture",
    )
    if stage == "execution":
        service.approve_plan(plan.plan_revision_id, approval, "atlas-approve")
    path = (
        root
        / {"aal": "aal.nii", "aal3": "AAL3v1_1mm.nii", "brodmann": "Brodmann_YCG.nii"}[atlas_id]
    )
    nib.save(nib.Nifti1Image(np.full((3, 4, 5), 2, dtype=np.int16), np.eye(4)), path)
    with pytest.raises(ConflictError) as failure:
        if stage == "validation":
            service.validate_statistical_design(
                plan.plan_revision_id,
                StatisticalDesignValidationRequest(expected_version=plan.version),
                "atlas-changed-validate",
            )
        elif stage == "approval":
            service.approve_plan(plan.plan_revision_id, approval, "atlas-approve")
        else:
            service.create_statistics_run(
                StatisticsRunCreate(
                    project_id=plan.project_id,
                    statistical_design_revision_id=plan.plan_revision_id,
                    expected_plan_hash=plan.plan_hash,
                ),
                "atlas-changed-run",
            )
    assert failure.value.code == (
        "plan_stale" if stage == "execution" else "statistics_atlas_changed"
    )
    assert (
        service.get_plan(plan.plan_revision_id).state.value
        == {
            "validation": "draft",
            "approval": "awaiting_approval",
            "execution": "approved",
        }[stage]
    )


def test_no_atlas_preserves_legacy_statistics_and_unavailable_selection_blocks(
    service, source_root, work_root
):
    _, request, _ = _proposal(service, source_root, work_root)
    with pytest.raises(InputValidationError, match="所选图谱不可用"):
        service.create_statistical_design(
            request.model_copy(update={"atlas_id": "aal"}), "atlas-missing"
        )
    created = service.create_statistical_design(request, "atlas-none")
    assert (
        created.localization_plan is None and "localization_plan" not in created.plan_revision.plan
    )


def _detail(selection, empty=False):
    manifest = StatisticalResultManifest(
        result_id="atlas-result",
        run_id="atlas-run",
        design_revision_id="atlas-design",
        mode="real",
        non_scientific=False,
        correction=None,
        cluster_connectivity_definition="26-neighbor fixture",
        artifacts=tuple(
            RegisteredArtifactMetadata(
                artifact_id=f"artifact-{role.value}",
                role=role,
                artifact_type=f"statistics.{role.value}",
                relative_path=f"output/{role.value}.nii",
                checksum_sha256="a" * 64,
                size_bytes=99,
                provenance_hash="b" * 64,
            )
            for role in StatisticalArtifactRole
            if role is not StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP
        ),
        clusters=()
        if empty
        else (
            ClusterRecord(
                cluster_id="cluster-1",
                source_map_artifact_id="artifact-uncorrected_statistical_map",
                extent_voxels=1,
                peak_statistic=4,
                peak_coordinate_mm=(0, 0, 0),
                coordinate_space="MNI",
            ),
        ),
    )
    return SimpleNamespace(
        manifest=manifest.model_dump(mode="json"),
        bundle_hash="c" * 64,
        report_json=json.dumps({"localization_plan": selection}),
    )


def test_localization_cannot_replace_frozen_atlas_but_empty_result_skips_atlas_checks(
    service, monkeypatch
):
    _atlases(service)
    selection = service._freeze_statistical_atlas("aal").model_dump(mode="json")
    monkeypatch.setattr(service.repository, "get_statistical_result", lambda _: _detail(selection))
    with pytest.raises(ConflictError) as failure:
        service.localize_statistical_result("atlas-result", "aal3")
    assert failure.value.code == "statistics_atlas_selection_mismatch"
    selection["atlas_content_hash"] = "e" * 64
    with pytest.raises(ConflictError) as failure:
        service.localize_statistical_result("atlas-result", "aal")
    assert failure.value.code == "statistics_atlas_changed"
    monkeypatch.setattr(
        service.repository, "get_statistical_result", lambda _: _detail(selection, empty=True)
    )
    assert (
        service.localize_statistical_result("atlas-result", "missing-atlas").status
        == "no_reportable_clusters"
    )
