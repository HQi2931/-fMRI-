"""Atomic completion assembly for real statistical jobs."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from neuroagent.application.hashing import content_hash
from neuroagent.application.reporting import build_statistical_reproducibility_report
from neuroagent.domain.fmri.results import (
    RegisteredArtifactMetadata,
    StatisticalArtifactRole,
    StatisticalResultManifest,
    StatisticalResultMode,
)
from neuroagent.domain.fmri.statistics import (
    CorrectionSpec,
    FdrCorrection,
    GrfCorrection,
    StatisticalDesignRevision,
)
from neuroagent.infrastructure.persistence.models import (
    ArtifactRow,
    PlanRevisionRow,
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
    )
    manifest = StatisticalResultManifest(
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
    report = build_statistical_reproducibility_report(
        manifest=manifest,
        design=design,
        correction=correction,
        qc_review_hash=design.qc_review_hash,
        environment_hash=plan.environment_hash,
        plan_hash=plan.plan_hash,
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
