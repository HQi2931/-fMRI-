"""Server-side compilation from frozen plans to typed MATLAB JobSpecs."""

from __future__ import annotations

from pathlib import PurePosixPath

from neuroagent.application.contracts import ArtifactView
from neuroagent.application.ports import RepositoryPort
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage
from neuroagent.domain.fmri.statistics import (
    CorrectionSpec,
    StatisticalDesignRevision,
    StatisticalQcSource,
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
from neuroagent.tools.dpabi_v82 import CorrectionFunction, DpabiV82Adapter


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
            StatisticalTest.CORRELATION,
            StatisticalTest.REGRESSION,
            StatisticalTest.ONE_WAY_ANOVA_ANCOVA,
        }:
            raise ValueError("unsupported DPABI statistical design")
        call = self._adapter.project_statistics(design)
        correction_call = self._adapter.project_correction(correction) if correction else None
        artifact_ids = tuple(
            dict.fromkeys(
                (*[image.artifact_id for image in design.images], design.mask_artifact_id)
            )
        )
        if design.qc_source is StatisticalQcSource.EXTERNAL_IMPORT:
            bindings = self._external_bindings(design, input_manifest_hash)
        else:
            mask_artifact = self._repository.get_artifact(design.mask_artifact_id)
            mask_lineage = self._lineage(mask_artifact.provenance, "statistical mask")
            if mask_lineage.kind is not ArtifactKind.BRAIN_MASK:
                raise ValueError("statistical mask artifact must carry brain-mask lineage")
            if mask_lineage.subject_manifest_hash != input_manifest_hash:
                raise ValueError(
                    "statistical mask lineage manifest does not match the frozen input"
                )
            for image in design.images:
                artifact = self._repository.get_artifact(image.artifact_id)
                lineage = self._lineage(artifact.provenance, "statistical image")
                if lineage.subject_id != image.subject_id:
                    raise ValueError(
                        "statistical image lineage subject does not match design order"
                    )
                if lineage.subject_manifest_hash != input_manifest_hash:
                    raise ValueError(
                        "statistical image lineage manifest does not match the frozen input"
                    )
                if not lineage.metadata_verified:
                    raise ValueError("statistical images require executor-verified NIfTI metadata")
                if not lineage.is_compatible_with_mask(design.mask_artifact_id):
                    raise ValueError(
                        "statistical image lineage is incompatible with the frozen mask"
                    )
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
            ExpectedArtifact(
                artifact_type="statistics.correction_evidence",
                relative_pattern="output/correction.json",
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
            if correction_call.function is CorrectionFunction.GRF:
                expected.append(
                    ExpectedArtifact(
                        artifact_type="statistics.corrected_z_map",
                        relative_pattern="output/statistic_corrected_z.nii",
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

    def _external_bindings(
        self, design: StatisticalDesignRevision, input_manifest_hash: str
    ) -> tuple[ArtifactPathBinding, ...]:
        assert design.external_import_id is not None
        imported = self._repository.get_external_analysis_import(design.external_import_id)
        selection = imported.selection
        inclusion = imported.inclusion
        if (
            imported.status != "included"
            or selection is None
            or inclusion is None
            or imported.inclusion_hash != design.external_import_hash
            or selection.input_manifest_hash != input_manifest_hash
            or selection.mask_artifact_id != design.mask_artifact_id
        ):
            raise ValueError("external statistics require an unchanged, included source and mask")
        if design.covariates and not design.external_demographics_revision_id:
            raise ValueError("external covariates require a frozen demographics revision")
        if design.external_demographics_revision_id:
            latest = self._repository.get_latest_external_demographics(imported.import_id)
            if (
                latest is None
                or latest.demographics_id != design.external_demographics_revision_id
                or latest.content_hash != design.external_demographics_hash
                or latest.inclusion_hash != design.external_import_hash
            ):
                raise ValueError("external demographics revision changed before execution")
        refs = {(item.subject_id, item.condition): item for item in selection.images}
        sources = {(item.subject_id, item.condition): item for item in imported.inspection.images}
        if {(item.subject_id, item.condition) for item in design.images} != set(refs):
            raise ValueError("external statistical pair identities differ from inclusion")
        if set(design.subject_order) != {item.subject_id for item in selection.images} or len(
            design.images
        ) != len(refs):
            raise ValueError("external statistical subject set differs from inclusion")
        bindings: list[ArtifactPathBinding] = []
        for image in design.images:
            ref = refs[(image.subject_id, image.condition)]
            source = sources[(image.subject_id, image.condition)]
            if image.artifact_id != ref.artifact_id:
                raise ValueError("external statistical image does not match its subject")
            if image.group is not None and image.group != ref.group:
                raise ValueError("external statistical group does not match its subject")
            bindings.append(
                ArtifactPathBinding(
                    artifact_id=image.artifact_id,
                    relative_path=f"input/{image.artifact_id}/{PurePosixPath(source.relative_path).name}",
                    read_only=True,
                    external_import_id=imported.import_id,
                    external_relative_path=source.relative_path,
                    external_sha256=source.sha256,
                    external_size_bytes=source.size_bytes,
                )
            )
        assert inclusion.mask_relative_path and inclusion.mask_sha256 and inclusion.mask_size_bytes
        bindings.append(
            ArtifactPathBinding(
                artifact_id=design.mask_artifact_id,
                relative_path=f"input/{design.mask_artifact_id}/{PurePosixPath(inclusion.mask_relative_path).name}",
                read_only=True,
                external_import_id=imported.import_id,
                external_relative_path=inclusion.mask_relative_path,
                external_sha256=inclusion.mask_sha256,
                external_size_bytes=inclusion.mask_size_bytes,
            )
        )
        return tuple(bindings)

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
