"""Server-side compilation from frozen plans to typed MATLAB JobSpecs."""

from __future__ import annotations

from pathlib import PurePosixPath

from neuroagent.application.contracts import ArtifactView
from neuroagent.application.ports import RepositoryPort
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.domain.fmri.statistics import (
    CorrectionSpec,
    StatisticalDesignRevision,
    StatisticalTest,
)
from neuroagent.execution.models import (
    ArtifactPathBinding,
    ExpectedArtifact,
    MatlabJobKind,
    MatlabJobSpec,
    PreprocessingJobPayload,
    StatisticsJobPayload,
)
from neuroagent.tools.dpabi_v82 import DpabiV82Adapter


class MatlabJobCompiler:
    """Compile only server-owned, approved data into an immutable JobSpec."""

    def __init__(self, repository: RepositoryPort) -> None:
        self._repository = repository
        self._adapter = DpabiV82Adapter()

    def compile_statistics(
        self,
        *,
        job_id: str,
        run_id: str,
        plan_hash: str,
        approval_record_id: str,
        input_manifest_hash: str,
        design: StatisticalDesignRevision,
        correction: CorrectionSpec | None,
    ) -> MatlabJobSpec:
        if design.test not in {
            StatisticalTest.ONE_SAMPLE_T,
            StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            StatisticalTest.PAIRED_T,
        }:
            raise ValueError("MATLAB v0.1 statistics supports only the three t-test designs")
        call = self._adapter.project_statistics(design)
        correction_call = self._adapter.project_correction(correction) if correction else None
        artifact_ids = tuple(
            dict.fromkeys(
                (*[image.artifact_id for image in design.images], design.mask_artifact_id)
            )
        )
        mask_artifact = self._repository.get_artifact(design.mask_artifact_id)
        mask_lineage = self._lineage(mask_artifact.provenance, "statistical mask")
        if mask_lineage.kind is not ArtifactKind.BRAIN_MASK:
            raise ValueError("statistical mask artifact must carry brain-mask lineage")
        if mask_lineage.subject_manifest_hash != input_manifest_hash:
            raise ValueError("statistical mask lineage manifest does not match the frozen input")
        for image in design.images:
            artifact = self._repository.get_artifact(image.artifact_id)
            lineage = self._lineage(artifact.provenance, "statistical image")
            if lineage.subject_id != image.subject_id:
                raise ValueError("statistical image lineage subject does not match design order")
            if lineage.subject_manifest_hash != input_manifest_hash:
                raise ValueError(
                    "statistical image lineage manifest does not match the frozen input"
                )
            if not lineage.metadata_verified:
                raise ValueError("statistical images require executor-verified NIfTI metadata")
            if not lineage.is_compatible_with_mask(design.mask_artifact_id):
                raise ValueError("statistical image lineage is incompatible with the frozen mask")
            if lineage.grid_signature != mask_lineage.grid_signature:
                raise ValueError("statistical image and mask grids do not match")
        bindings = tuple(
            self._binding(self._repository.get_artifact(item)) for item in artifact_ids
        )
        expected = [
            ExpectedArtifact(
                artifact_type="statistics.design_matrix",
                relative_pattern="output/design_matrix.json",
                required=True,
            ),
            ExpectedArtifact(
                artifact_type="statistics.contrast",
                relative_pattern="output/contrast.json",
                required=True,
            ),
            ExpectedArtifact(
                artifact_type="statistics.uncorrected_statistical_map",
                relative_pattern="output/statistic.nii",
                required=True,
            ),
            ExpectedArtifact(
                artifact_type="statistics.effect_map",
                relative_pattern="output/effect.nii",
                required=True,
            ),
            ExpectedArtifact(
                artifact_type="statistics.cluster_table",
                relative_pattern="output/clusters.tsv",
                required=True,
            ),
            ExpectedArtifact(
                artifact_type="statistics.execution_log",
                relative_pattern="logs/matlab.log",
                required=True,
            ),
            ExpectedArtifact(
                artifact_type="statistics.software_version_evidence",
                relative_pattern="output/software-version.json",
                required=True,
            ),
        ]
        if correction_call is not None:
            expected.append(
                ExpectedArtifact(
                    artifact_type="statistics.corrected_statistical_map",
                    relative_pattern="output/statistic_corrected.nii",
                    required=True,
                )
            )
        return MatlabJobSpec(
            job_id=job_id,
            run_id=run_id,
            kind=MatlabJobKind.DPABI_STATISTICS,
            plan_hash=plan_hash,
            approval_record_id=approval_record_id,
            input_manifest_hash=input_manifest_hash,
            timeout_seconds=43_200,
            artifact_bindings=bindings,
            expected_artifacts=tuple(expected),
            payload=StatisticsJobPayload(statistics=call, correction=correction_call),
        )

    def _binding(self, artifact: ArtifactView) -> ArtifactPathBinding:
        name = PurePosixPath(artifact.relative_path).name
        return ArtifactPathBinding(
            artifact_id=artifact.artifact_id,
            relative_path=f"input/{artifact.artifact_id}/{name}",
            read_only=True,
        )

    @staticmethod
    def _lineage(provenance: dict[str, object], label: str) -> ArtifactLineage:
        value = provenance.get("lineage")
        if not isinstance(value, dict):
            raise ValueError(f"{label} is missing typed Artifact lineage")
        try:
            return ArtifactLineage.model_validate(value)
        except ValueError as exc:
            raise ValueError(f"{label} lineage is invalid") from exc


def compile_preprocessing_job(
    *,
    job_id: str,
    run_id: str,
    plan_hash: str,
    approval_record_id: str,
    input_manifest_hash: str,
    payload: PreprocessingJobPayload,
    bindings: tuple[ArtifactPathBinding, ...],
) -> MatlabJobSpec:
    """Build a preprocessing spec from a server-generated typed projection."""

    return MatlabJobSpec(
        job_id=job_id,
        run_id=run_id,
        kind=MatlabJobKind.DPARSFA_PREPROCESSING,
        plan_hash=plan_hash,
        approval_record_id=approval_record_id,
        input_manifest_hash=input_manifest_hash,
        timeout_seconds=86_400,
        artifact_bindings=bindings,
        expected_artifacts=(
            ExpectedArtifact(
                artifact_type="preprocessing.metric_or_timeseries",
                relative_pattern="output/**/*",
                required=True,
            ),
        ),
        payload=payload,
    )
