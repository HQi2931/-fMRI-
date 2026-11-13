"""Atomic completion assembly for real statistical jobs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

import nibabel as nib
import numpy as np
from sqlalchemy.orm import Session

from neuroagent.analysis.clusters import parse_cluster_table
from neuroagent.application.hashing import content_hash
from neuroagent.application.reporting import build_statistical_reproducibility_report
from neuroagent.domain.fmri.results import (
    ClusterRecord as ResultClusterRecord,
)
from neuroagent.domain.fmri.results import (
    RegisteredArtifactMetadata,
    StatisticalArtifactRole,
    StatisticalAtlasSelection,
    StatisticalResultManifest,
    StatisticalResultMode,
)
from neuroagent.domain.fmri.statistics import (
    CorrectionSpec,
    FdrCorrection,
    GrfCorrection,
    StatisticalDesignRevision,
    StatisticalQcSource,
)
from neuroagent.infrastructure.persistence.models import (
    ArtifactRow,
    ExternalAnalysisImportRow,
    PlanRevisionRow,
    ProjectRow,
    RuntimeEventRow,
    StatisticalResultRow,
    WorkflowRunRow,
)
from neuroagent.infrastructure.persistence.repository_mixins._base import _load
from neuroagent.observability.events import redact_event_payload
from neuroagent.observability.tracing import current_trace_id

_ROLE_BY_ARTIFACT_TYPE = {
    "statistics.design_matrix": StatisticalArtifactRole.DESIGN_MATRIX,
    "statistics.contrast": StatisticalArtifactRole.CONTRAST,
    "statistics.uncorrected_statistical_map": StatisticalArtifactRole.UNCORRECTED_STATISTICAL_MAP,
    "statistics.corrected_statistical_map": StatisticalArtifactRole.CORRECTED_STATISTICAL_MAP,
    "statistics.effect_map": StatisticalArtifactRole.EFFECT_MAP,
    "statistics.cluster_table": StatisticalArtifactRole.CLUSTER_TABLE,
    "statistics.execution_log": StatisticalArtifactRole.EXECUTION_LOG,
    "statistics.software_version_evidence": StatisticalArtifactRole.SOFTWARE_VERSION_EVIDENCE,
    "statistics.correction_evidence": StatisticalArtifactRole.CORRECTION_EVIDENCE,
    "statistics.corrected_z_map": StatisticalArtifactRole.CORRECTED_Z_MAP,
    "statistics.execution_script": StatisticalArtifactRole.EXECUTION_SCRIPT,
    "statistics.execution_bootstrap": StatisticalArtifactRole.EXECUTION_BOOTSTRAP,
    "statistics.job_configuration": StatisticalArtifactRole.JOB_CONFIGURATION,
    "statistics.input_manifest": StatisticalArtifactRole.INPUT_MANIFEST,
}


def register_real_statistical_result(
    session: Session,
    *,
    run: WorkflowRunRow,
    plan: PlanRevisionRow,
    payload: dict[str, Any],
    artifact_rows: list[ArtifactRow],
    actor: str,
    created_at: datetime,
) -> tuple[StatisticalResultRow, RuntimeEventRow]:
    """Build and stage the real result row and event in the caller's transaction."""

    design = StatisticalDesignRevision.model_validate(payload.get("statistical_design"))
    if plan.plan_hash != str(payload.get("plan_hash")):
        raise ValueError("statistical completion plan hash does not match the run payload")
    frozen_plan_payload = _load(plan.plan_json, {})
    if not isinstance(frozen_plan_payload, dict) or not isinstance(
        frozen_plan_payload.get("design"), dict
    ):
        raise ValueError("frozen statistical plan does not contain a typed design")
    frozen_plan_design = StatisticalDesignRevision.model_validate(frozen_plan_payload["design"])
    if content_hash(frozen_plan_design.model_dump(mode="json")) != content_hash(
        design.model_dump(mode="json")
    ):
        raise ValueError("statistical completion design does not match the frozen plan")
    correction = _parse_correction(payload.get("correction"))
    expected_manifest_hash = str(payload.get("input_manifest_hash"))
    if expected_manifest_hash != plan.manifest_hash:
        raise ValueError("statistical completion input manifest hash drifted")

    result_id = f"{run.run_id}-statistical-result"
    registered = tuple(
        _registered_artifact(row)
        for row in sorted(artifact_rows, key=lambda item: item.artifact_id)
        if row.artifact_type != "execution.matlab_log"
    )
    # Validate the artifact contract before opening any result files. This gives
    # callers a deterministic "incomplete result" error when a worker omitted a
    # required output, while still checking file integrity before registration.
    StatisticalResultManifest(
        result_id=result_id,
        run_id=run.run_id,
        design_revision_id=design.revision_id,
        mode=StatisticalResultMode.REAL,
        non_scientific=False,
        non_scientific_reason=None,
        correction=correction,
        cluster_connectivity_definition=(
            "26-neighbor voxel connectivity; coordinates from NIfTI affine"
        ),
        artifacts=registered,
        clusters=(),
    )
    if correction is not None:
        required_roles = {StatisticalArtifactRole.CORRECTION_EVIDENCE}
        if isinstance(correction, GrfCorrection):
            required_roles.add(StatisticalArtifactRole.CORRECTED_Z_MAP)
        if required_roles - {item.role for item in registered}:
            raise ValueError("incomplete statistical correction evidence or GRF Z map")
    cluster_table = next(
        (row for row in artifact_rows if row.artifact_type == "statistics.cluster_table"), None
    )
    uncorrected_map = next(
        (
            row
            for row in artifact_rows
            if row.artifact_type == "statistics.uncorrected_statistical_map"
        ),
        None,
    )
    corrected_map = next(
        (
            row
            for row in artifact_rows
            if row.artifact_type == "statistics.corrected_statistical_map"
        ),
        None,
    )
    if cluster_table is None or (corrected_map if correction else uncorrected_map) is None:
        raise ValueError("statistical result is missing the cluster table or its source map")
    project = session.get(ProjectRow, run.project_id)
    if project is None:
        raise ValueError("statistical result project is unavailable")
    reproducibility_types = {
        "statistics.execution_script",
        "statistics.execution_bootstrap",
        "statistics.job_configuration",
        "statistics.input_manifest",
    }
    present_reproducibility_types = {
        row.artifact_type for row in artifact_rows
    } & reproducibility_types
    if present_reproducibility_types and present_reproducibility_types != reproducibility_types:
        raise ValueError("statistical execution evidence is incomplete")
    for artifact in artifact_rows:
        if artifact.artifact_type in reproducibility_types:
            _verified_artifact_path(project, run.run_id, artifact)
    if correction is not None:
        for artifact in artifact_rows:
            if artifact.artifact_type in {
                "statistics.correction_evidence",
                "statistics.corrected_z_map",
            }:
                _verified_artifact_path(project, run.run_id, artifact)
    cluster_table_path = _verified_artifact_path(project, run.run_id, cluster_table)
    parsed_clusters = parse_cluster_table(cluster_table_path)
    if _sha256(cluster_table_path) != cluster_table.checksum:
        raise ValueError("statistical cluster table changed during result registration")
    significant_voxel_count: int | None = None
    if correction is not None:
        assert corrected_map is not None
        corrected_map_path = _verified_artifact_path(project, run.run_id, corrected_map)
        corrected_image = cast(nib.Nifti1Image, nib.load(str(corrected_map_path)))
        corrected_values = corrected_image.get_fdata(dtype=np.float32, caching="unchanged")
        significant_voxel_count = int(
            np.count_nonzero(np.isfinite(corrected_values) & (corrected_values != 0))
        )
        cluster_voxel_count = sum(item.voxel_count or 0 for item in parsed_clusters)
        if any(item.voxel_count is None for item in parsed_clusters) or (
            significant_voxel_count != cluster_voxel_count
        ):
            raise ValueError(
                "corrected statistical map voxel count does not match the registered cluster table"
            )

    input_artifact = session.get(ArtifactRow, design.images[0].artifact_id)
    input_lineage = (
        _load(input_artifact.provenance_json, {}).get("lineage", {})
        if input_artifact is not None
        else {}
    )
    coordinate_space = (
        str(input_lineage.get("space"))
        if isinstance(input_lineage, dict) and input_lineage.get("space")
        else "unknown"
    )
    if design.qc_source is StatisticalQcSource.EXTERNAL_IMPORT:
        imported = session.get(ExternalAnalysisImportRow, design.external_import_id)
        if (
            imported is None
            or imported.project_id != run.project_id
            or imported.status != "included"
            or imported.inclusion_hash != design.external_import_hash
        ):
            raise ValueError("statistical completion external source evidence differs from design")
        coordinate_space = str(
            _load(imported.inclusion_json, {}).get("coordinate_space", "unknown")
        )
    source_map = corrected_map if correction else uncorrected_map
    assert source_map is not None
    clusters = tuple(
        _result_cluster(item, source_map.artifact_id, coordinate_space) for item in parsed_clusters
    )

    manifest = StatisticalResultManifest(
        result_id=result_id,
        run_id=run.run_id,
        design_revision_id=design.revision_id,
        mode=StatisticalResultMode.REAL,
        non_scientific=False,
        non_scientific_reason=None,
        correction=correction,
        significant_voxel_count=significant_voxel_count,
        cluster_connectivity_definition=(
            "26-neighbor voxel connectivity; coordinates from NIfTI affine"
        ),
        artifacts=registered,
        clusters=clusters,
    )
    report = build_statistical_reproducibility_report(
        manifest=manifest,
        design=design,
        correction=correction,
        qc_review_hash=design.qc_review_hash,
        environment_hash=plan.environment_hash,
        plan_hash=plan.plan_hash,
        localization_plan=(
            StatisticalAtlasSelection.model_validate(frozen_plan_payload["localization_plan"])
            if frozen_plan_payload.get("localization_plan") is not None
            else None
        ),
    )
    row = StatisticalResultRow(
        result_id=result_id,
        project_id=run.project_id,
        run_id=run.run_id,
        design_revision_id=design.revision_id,
        mode=manifest.mode.value,
        non_scientific=False,
        non_scientific_reason=None,
        bundle_hash=report.bundle_hash,
        manifest_json=manifest.model_dump_json(),
        report_markdown=report.markdown,
        report_json=report.json_text,
        version=1,
        created_at=created_at,
    )
    event = RuntimeEventRow(
        trace_id=current_trace_id(),
        project_id=run.project_id,
        run_id=run.run_id,
        event_type="StatisticalResultRegistered",
        severity="info",
        payload_json=json.dumps(
            redact_event_payload(
                {
                    "actor": actor,
                    "result_id": result_id,
                    "design_revision_id": design.revision_id,
                    "mode": manifest.mode.value,
                    "bundle_hash": report.bundle_hash,
                    "synthetic": False,
                }
            ),
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    session.add(row)
    session.add(event)
    return row, event


def _verified_artifact_path(project: ProjectRow, run_id: str, artifact: ArtifactRow) -> Path:
    relative = PurePosixPath(artifact.relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or ":" in artifact.relative_path
        or "\\" in artifact.relative_path
        or relative.as_posix() != artifact.relative_path
    ):
        raise ValueError("statistical artifact path is not a canonical run-relative path")
    run_root = Path(project.work_root) / run_id
    candidate = run_root.joinpath(*relative.parts)
    try:
        for entry in (run_root, *candidate.parents, candidate):
            is_junction = getattr(entry, "is_junction", lambda: False)
            if entry.is_symlink() or is_junction():
                raise ValueError("statistical artifact path contains a link")
        resolved_root = run_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
            raise ValueError("statistical artifact path escapes its run directory")
        if resolved.stat().st_size != artifact.size_bytes or _sha256(resolved) != artifact.checksum:
            raise ValueError("statistical artifact integrity changed")
        return resolved
    except OSError as exc:
        raise ValueError("statistical artifact file is unavailable") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _result_cluster(
    item: Any, source_map_artifact_id: str, coordinate_space: str
) -> ResultClusterRecord:
    if item.voxel_count is None or item.statistic is None:
        raise ValueError("statistical cluster table is missing extent or peak statistic")
    return ResultClusterRecord(
        cluster_id=item.cluster_id,
        source_map_artifact_id=source_map_artifact_id,
        extent_voxels=item.voxel_count,
        peak_statistic=item.statistic,
        peak_coordinate_mm=(item.peak_x, item.peak_y, item.peak_z),
        coordinate_space=coordinate_space,
        inference=item.inference,
        direction=item.direction,
    )


def _registered_artifact(row: ArtifactRow) -> RegisteredArtifactMetadata:
    role = _ROLE_BY_ARTIFACT_TYPE.get(row.artifact_type)
    if role is None:
        raise ValueError(f"unexpected statistical artifact type: {row.artifact_type}")
    provenance = _load(row.provenance_json, {})
    return RegisteredArtifactMetadata(
        artifact_id=row.artifact_id,
        role=role,
        artifact_type=row.artifact_type,
        relative_path=row.relative_path,
        checksum_sha256=row.checksum,
        size_bytes=row.size_bytes,
        provenance_hash=content_hash(provenance),
        placeholder=False,
    )


def _parse_correction(value: object) -> CorrectionSpec | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("correction payload must be an object")
    if value.get("method") == "fdr":
        return FdrCorrection.model_validate(value)
    if value.get("method") == "grf":
        return GrfCorrection.model_validate(value)
    raise ValueError("unsupported correction method")
