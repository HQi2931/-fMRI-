"""Fail-closed guards in the frozen-plan to MATLAB JobSpec compiler.

``MatlabJobCompiler.compile_statistics`` is the last check before a real MATLAB
statistics job is compiled from an approved plan.  Every branch exercised below
is a refusal path: the design, the frozen input manifest and the registered
artifact lineage must agree before a JobSpec exists at all.  These tests pin the
refusals so a relaxation cannot slip in unnoticed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from neuroagent.application.contracts.runs import (
    ArtifactView,
)
from neuroagent.domain.fmri import (
    AnalysisImage,
    ArtifactKind,
    ArtifactLineage,
    Centering,
    CovariateColumn,
    CovariateValue,
    FdrCorrection,
    MetricScaling,
    MissingValuePolicy,
    StatisticalDesignRevision,
    StatisticalMapType,
    StatisticalTest,
    Tail,
)
from neuroagent.execution.compiler import MatlabJobCompiler
from neuroagent.execution.models import MatlabJobKind

MANIFEST_HASH = "a" * 64
MASK_ID = "mask-001"
GRID = "grid-3mm"
SUBJECTS = ("sub-01", "sub-02", "sub-03")


class FakeRepository:
    """The single repository operation the compiler is allowed to need."""

    def __init__(self, artifacts: dict[str, ArtifactView]) -> None:
        self._artifacts = artifacts

    def get_artifact(self, artifact_id: str) -> ArtifactView:
        return self._artifacts[artifact_id]


def artifact(artifact_id: str, lineage: ArtifactLineage | None) -> ArtifactView:
    provenance: dict[str, Any] = (
        {} if lineage is None else {"lineage": lineage.model_dump(mode="json")}
    )
    return ArtifactView(
        artifact_id=artifact_id,
        project_id="project-1",
        run_id="run-1",
        artifact_type="metric.alff",
        relative_path=f"output/{artifact_id}.nii",
        checksum="c" * 64,
        size_bytes=1024,
        provenance=provenance,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def image_lineage(subject_id: str, **overrides: Any) -> ArtifactLineage:
    """A verified subject-level metric lineage that satisfies the compiler."""

    values: dict[str, Any] = {
        "artifact_id": f"image-{subject_id}",
        "kind": ArtifactKind.ALFF_MAP,
        "subject_id": subject_id,
        "metric_scaling": MetricScaling.RAW,
        "metadata_verified": True,
        "tr_seconds": 2.0,
        "volume_count": 120,
        "metadata_evidence_hash": "e" * 64,
        "subject_manifest_hash": MANIFEST_HASH,
        "space": "MNI152",
        "grid_signature": GRID,
        "voxel_size_mm": (3.0, 3.0, 3.0),
        "mask_artifact_id": MASK_ID,
        "mask_grid_signature": GRID,
        "temporally_filtered": False,
        "frequency_band": None,
        "spatially_smoothed": False,
        "smoothing_fwhm_mm": None,
        "scrubbed": False,
        "producer_step_hash": "b" * 64,
    }
    values.update(overrides)
    return ArtifactLineage(**values)


def mask_lineage(**overrides: Any) -> ArtifactLineage:
    values: dict[str, Any] = {
        "artifact_id": MASK_ID,
        "kind": ArtifactKind.BRAIN_MASK,
        "metadata_verified": False,
        "subject_manifest_hash": MANIFEST_HASH,
        "space": "MNI152",
        "grid_signature": GRID,
        "voxel_size_mm": (3.0, 3.0, 3.0),
        "mask_artifact_id": None,
        "mask_grid_signature": None,
        "temporally_filtered": False,
        "frequency_band": None,
        "spatially_smoothed": False,
        "smoothing_fwhm_mm": None,
        "scrubbed": False,
        "producer_step_hash": "d" * 64,
    }
    values.update(overrides)
    return ArtifactLineage(**values)


def design(*, test: StatisticalTest = StatisticalTest.ONE_SAMPLE_T) -> StatisticalDesignRevision:
    return StatisticalDesignRevision(
        revision_id="stats-1",
        test=test,
        subject_order=SUBJECTS,
        images=tuple(
            AnalysisImage(
                subject_id=subject_id,
                artifact_id=f"image-{subject_id}",
                group=None,
                condition=None,
            )
            for subject_id in SUBJECTS
        ),
        group_order=(),
        condition_order=(),
        covariates=(
            CovariateColumn(
                name="age",
                values=tuple(
                    CovariateValue(subject_id=subject_id, value=value)
                    for subject_id, value in zip(SUBJECTS, (20.0, 30.0, 40.0), strict=True)
                ),
                centering=Centering.GRAND_MEAN,
            ),
        ),
        contrast=(1.0, 0.0),
        one_sample_baseline=0.0,
        mask_artifact_id=MASK_ID,
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id="qc-1",
        qc_review_hash="q" * 64,
    )


def repository(
    *,
    images: dict[str, ArtifactLineage] | None = None,
    mask: ArtifactLineage | None = None,
    mask_artifact: ArtifactView | None = None,
) -> FakeRepository:
    if mask_artifact is None:
        mask_artifact = artifact(MASK_ID, mask if mask is not None else mask_lineage())
    artifacts: dict[str, ArtifactView] = {
        MASK_ID: mask_artifact,
        "mask-OTHER": artifact("mask-OTHER", mask_lineage(artifact_id="mask-OTHER")),
    }
    for artifact_id, lineage in (images or {}).items():
        artifacts[artifact_id] = artifact(artifact_id, lineage)
    return FakeRepository(artifacts)


def compiler(**kwargs: Any) -> MatlabJobCompiler:
    return MatlabJobCompiler(repository(**kwargs))


def test_compiles_bindings_expected_artifacts_and_correction() -> None:
    mask = mask_lineage()
    job = compiler(
        images={f"image-{subject}": image_lineage(subject) for subject in SUBJECTS},
        mask=mask,
    ).compile_statistics(
        job_id="job-1",
        run_id="run-1",
        plan_hash="p" * 64,
        approval_record_id="approval-1",
        input_manifest_hash=MANIFEST_HASH,
        design=design(),
        correction=FdrCorrection(
            method="fdr",
            q_threshold=0.05,
            mask_artifact_id=MASK_ID,
            statistic_type=StatisticalMapType.T,
            df1=1,
            df2=None,
        ),
    )

    assert job.kind is MatlabJobKind.DPABI_STATISTICS
    assert job.input_manifest_hash == MANIFEST_HASH
    assert job.plan_hash == "p" * 64
    # Every image plus the mask is bound read-only, in a server-owned layout.
    assert {binding.artifact_id for binding in job.artifact_bindings} == {
        *(f"image-{subject}" for subject in SUBJECTS),
        MASK_ID,
    }
    assert all(binding.read_only for binding in job.artifact_bindings)
    assert all(binding.relative_path.startswith("input/") for binding in job.artifact_bindings)
    patterns = {item.relative_pattern for item in job.expected_artifacts}
    assert "output/design_matrix.json" in patterns
    assert "output/clusters.tsv" in patterns
    assert "output/software-version.json" in patterns
    # A correction adds the corrected map to the required outputs.
    assert "output/statistic_corrected.nii" in patterns


def test_uncorrected_design_does_not_require_a_corrected_map() -> None:
    job = compiler(
        images={f"image-{subject}": image_lineage(subject) for subject in SUBJECTS}
    ).compile_statistics(
        job_id="job-1",
        run_id="run-1",
        plan_hash="p" * 64,
        approval_record_id="approval-1",
        input_manifest_hash=MANIFEST_HASH,
        design=design(),
        correction=None,
    )

    assert "output/statistic_corrected.nii" not in {
        item.relative_pattern for item in job.expected_artifacts
    }


def test_compiles_regression_design_to_the_dpabi_group_analysis_entry() -> None:
    # Regression is part of the real MATLAB-backed DPABI capability set.
    unsupported = design().model_copy(update={"test": StatisticalTest.REGRESSION})

    job = compiler(
        images={f"image-{subject}": image_lineage(subject) for subject in SUBJECTS}
    ).compile_statistics(
        job_id="job-1",
        run_id="run-1",
        plan_hash="p" * 64,
        approval_record_id="approval-1",
        input_manifest_hash=MANIFEST_HASH,
        design=unsupported,
        correction=None,
    )

    assert job.payload.statistics.function.value == "y_GroupAnalysis_Image"


def test_rejects_mask_that_does_not_carry_brain_mask_lineage() -> None:
    with pytest.raises(ValueError, match="brain-mask lineage"):
        compiler(
            images={f"image-{subject}": image_lineage(subject) for subject in SUBJECTS},
            mask=mask_lineage(kind=ArtifactKind.QC_REPORT),
        ).compile_statistics(
            job_id="job-1",
            run_id="run-1",
            plan_hash="p" * 64,
            approval_record_id="approval-1",
            input_manifest_hash=MANIFEST_HASH,
            design=design(),
            correction=None,
        )


def test_rejects_mask_bound_to_a_different_frozen_manifest() -> None:
    with pytest.raises(ValueError, match="mask lineage manifest"):
        compiler(
            images={f"image-{subject}": image_lineage(subject) for subject in SUBJECTS},
            mask=mask_lineage(subject_manifest_hash="f" * 64),
        ).compile_statistics(
            job_id="job-1",
            run_id="run-1",
            plan_hash="p" * 64,
            approval_record_id="approval-1",
            input_manifest_hash=MANIFEST_HASH,
            design=design(),
            correction=None,
        )


def test_rejects_image_from_a_different_subject_than_the_design() -> None:
    images = {f"image-{subject}": image_lineage(subject) for subject in SUBJECTS}
    images["image-sub-02"] = image_lineage("sub-99")

    with pytest.raises(ValueError, match="subject does not match design order"):
        compiler(images=images).compile_statistics(
            job_id="job-1",
            run_id="run-1",
            plan_hash="p" * 64,
            approval_record_id="approval-1",
            input_manifest_hash=MANIFEST_HASH,
            design=design(),
            correction=None,
        )


def test_rejects_unverified_image_metadata() -> None:
    # FUNCTIONAL_TIMESERIES is the only kind that may legally be unverified.
    unverified = image_lineage(
        "sub-01",
        kind=ArtifactKind.FUNCTIONAL_TIMESERIES,
        metric_scaling=None,
        metadata_verified=False,
        tr_seconds=None,
        volume_count=None,
        metadata_evidence_hash=None,
    )

    with pytest.raises(ValueError, match="executor-verified NIfTI metadata"):
        compiler(images={"image-sub-01": unverified}).compile_statistics(
            job_id="job-1",
            run_id="run-1",
            plan_hash="p" * 64,
            approval_record_id="approval-1",
            input_manifest_hash=MANIFEST_HASH,
            design=design(),
            correction=None,
        )


def test_rejects_image_bound_to_a_different_mask_artifact() -> None:
    images = {f"image-{subject}": image_lineage(subject) for subject in SUBJECTS}
    images["image-sub-01"] = image_lineage("sub-01", mask_artifact_id="mask-OTHER")

    with pytest.raises(ValueError, match="incompatible with the frozen mask"):
        compiler(images=images).compile_statistics(
            job_id="job-1",
            run_id="run-1",
            plan_hash="p" * 64,
            approval_record_id="approval-1",
            input_manifest_hash=MANIFEST_HASH,
            design=design(),
            correction=None,
        )


def test_rejects_image_whose_grid_does_not_match_the_mask() -> None:
    images = {f"image-{subject}": image_lineage(subject) for subject in SUBJECTS}
    # Still a valid lineage (its own mask agrees with its own grid) but it is a
    # different grid from the frozen mask artifact.
    images["image-sub-01"] = image_lineage(
        "sub-01", grid_signature="grid-2mm", mask_grid_signature="grid-2mm"
    )

    with pytest.raises(ValueError, match="grids do not match"):
        compiler(images=images).compile_statistics(
            job_id="job-1",
            run_id="run-1",
            plan_hash="p" * 64,
            approval_record_id="approval-1",
            input_manifest_hash=MANIFEST_HASH,
            design=design(),
            correction=None,
        )


def test_rejects_artifact_without_typed_lineage() -> None:
    with pytest.raises(ValueError, match="missing typed Artifact lineage"):
        compiler(
            images={f"image-{subject}": image_lineage(subject) for subject in SUBJECTS},
            mask_artifact=artifact(MASK_ID, None),
        ).compile_statistics(
            job_id="job-1",
            run_id="run-1",
            plan_hash="p" * 64,
            approval_record_id="approval-1",
            input_manifest_hash=MANIFEST_HASH,
            design=design(),
            correction=None,
        )


def test_rejects_malformed_lineage_payload() -> None:
    broken = artifact(MASK_ID, None).model_copy(
        update={"provenance": {"lineage": {"artifact_id": MASK_ID}}}
    )

    with pytest.raises(ValueError, match="lineage is invalid"):
        compiler(
            images={f"image-{subject}": image_lineage(subject) for subject in SUBJECTS},
            mask_artifact=broken,
        ).compile_statistics(
            job_id="job-1",
            run_id="run-1",
            plan_hash="p" * 64,
            approval_record_id="approval-1",
            input_manifest_hash=MANIFEST_HASH,
            design=design(),
            correction=None,
        )
