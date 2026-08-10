"""Deterministic statistical reproducibility reports without model calls."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from neuroagent.application.hashing import content_hash
from neuroagent.domain.fmri.results import (
    ClusterRecord,
    RegisteredArtifactMetadata,
    StatisticalResultManifest,
    StatisticalResultMode,
)
from neuroagent.domain.fmri.statistics import (
    CorrectionSpec,
    StatisticalDesignRevision,
    design_matrix,
    validate_correction_for_design,
)

SYNTHETIC_WARNING = (
    "SYNTHETIC / NON-SCIENTIFIC RESULT: this workflow fixture must not be used "
    "for scientific or clinical inference."
)
RESEARCH_USE_NOTICE = (
    "Research use only; this report is not a clinical diagnosis. Registered evidence, "
    "statistical assumptions, and scientific interpretation require independent review."
)


@dataclass(frozen=True, slots=True)
class StatisticalReproducibilityReport:
    """Two deterministic renderings of one canonical report payload."""

    markdown: str
    json_text: str
    bundle_hash: str


def build_statistical_reproducibility_report(
    *,
    manifest: StatisticalResultManifest,
    design: StatisticalDesignRevision,
    correction: CorrectionSpec | None,
    qc_review_hash: str,
    environment_hash: str,
    plan_hash: str,
) -> StatisticalReproducibilityReport:
    """Build a stable Markdown and JSON report from frozen, registered evidence."""

    _require_sha256("QC review", qc_review_hash)
    _require_sha256("environment", environment_hash)
    _require_sha256("plan", plan_hash)
    if design.qc_review_hash != qc_review_hash:
        raise ValueError("report QC hash does not match the frozen statistical design")
    if manifest.design_revision_id != design.revision_id:
        raise ValueError("result manifest does not reference the frozen design revision")
    if not _same_correction(manifest.correction, correction):
        raise ValueError("result manifest CorrectionSpec does not match the report input")
    validate_correction_for_design(design, correction)

    ordered_artifacts = tuple(
        sorted(manifest.artifacts, key=lambda item: (item.role.value, item.artifact_id))
    )
    ordered_clusters = tuple(sorted(manifest.clusters, key=lambda item: item.cluster_id))
    design_payload = design.model_dump(mode="json")
    correction_payload = correction.model_dump(mode="json") if correction is not None else None
    artifact_payload = [artifact.model_dump(mode="json") for artifact in ordered_artifacts]
    cluster_payload = [cluster.model_dump(mode="json") for cluster in ordered_clusters]
    synthetic = manifest.mode is StatisticalResultMode.SYNTHETIC_NON_SCIENTIFIC

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "generator": "neuroagent.application.reporting",
        "result": {
            "result_id": manifest.result_id,
            "run_id": manifest.run_id,
            "mode": manifest.mode.value,
            "non_scientific": manifest.non_scientific,
            "non_scientific_reason": manifest.non_scientific_reason,
            "warning": SYNTHETIC_WARNING if synthetic else None,
        },
        "frozen_bindings": {
            "design_revision_id": design.revision_id,
            "design_hash": content_hash(design_payload),
            "qc_review_revision_id": design.qc_review_revision_id,
            "qc_review_hash": qc_review_hash,
            "environment_hash": environment_hash,
            "plan_hash": plan_hash,
        },
        "statistical_design": design_payload,
        "design_matrix": design_matrix(design),
        "contrast": design.contrast,
        "correction": correction_payload,
        "cluster_connectivity_definition": manifest.cluster_connectivity_definition,
        "artifacts": artifact_payload,
        "artifact_manifest_hash": content_hash(artifact_payload),
        "clusters": cluster_payload,
        "limitations": [
            RESEARCH_USE_NOTICE,
            "Effect-map semantics are not inferred by this report contract; consult the "
            "registered method and version evidence.",
            "Cluster connectivity is reported only from the explicit manifest value; no "
            "connectivity default is inferred.",
        ],
    }
    json_text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    markdown = _render_markdown(payload, manifest, ordered_artifacts, ordered_clusters)
    bundle_hash = hashlib.sha256(f"{json_text}\n{markdown}".encode()).hexdigest()
    return StatisticalReproducibilityReport(
        markdown=markdown,
        json_text=json_text,
        bundle_hash=bundle_hash,
    )


def _same_correction(left: CorrectionSpec | None, right: CorrectionSpec | None) -> bool:
    if left is None or right is None:
        return left is right
    return left.model_dump(mode="json") == right.model_dump(mode="json")


def _require_sha256(label: str, value: str) -> None:
    if re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError(f"{label} hash must be a lowercase SHA-256 value")


def _render_markdown(
    payload: dict[str, Any],
    manifest: StatisticalResultManifest,
    artifacts: tuple[RegisteredArtifactMetadata, ...],
    clusters: tuple[ClusterRecord, ...],
) -> str:
    bindings = payload["frozen_bindings"]
    design = payload["statistical_design"]
    correction = payload["correction"]
    lines = ["# Statistical reproducibility report", ""]
    if manifest.mode is StatisticalResultMode.SYNTHETIC_NON_SCIENTIFIC:
        lines.extend(
            [
                f"> **{SYNTHETIC_WARNING}**",
                ">",
                f"> Reason: {_md_cell(manifest.non_scientific_reason or '')}",
                "",
            ]
        )
    lines.extend(
        [
            f"> {RESEARCH_USE_NOTICE}",
            "",
            "## Frozen evidence bindings",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Result ID | {_md_cell(manifest.result_id)} |",
            f"| Run ID | {_md_cell(manifest.run_id)} |",
            f"| Result mode | `{manifest.mode.value}` |",
            f"| Design revision | {_md_cell(str(bindings['design_revision_id']))} |",
            f"| Design hash | `{bindings['design_hash']}` |",
            f"| QC review hash | `{bindings['qc_review_hash']}` |",
            f"| Environment hash | `{bindings['environment_hash']}` |",
            f"| Plan hash | `{bindings['plan_hash']}` |",
            "",
            "## Statistical design",
            "",
            f"- Test: `{design['test']}`",
            f"- Tail: `{design['tail']}`",
            f"- Frozen subject order: `{json.dumps(design['subject_order'], ensure_ascii=False)}`",
            f"- Contrast: `{json.dumps(payload['contrast'], ensure_ascii=False)}`",
            "- Design matrix:",
            "",
            "```json",
            json.dumps(payload["design_matrix"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## Multiple-comparison correction",
            "",
        ]
    )
    if correction is None:
        lines.append("No correction was declared in the frozen result contract.")
    else:
        lines.extend(
            [
                "```json",
                json.dumps(correction, ensure_ascii=False, sort_keys=True, indent=2),
                "```",
            ]
        )
    lines.extend(
        [
            "",
            "## Registered artifacts",
            "",
            "| Role | Artifact ID | Type | Relative path | SHA-256 | Size (bytes) | Placeholder |",
            "| --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for artifact in artifacts:
        checksum = artifact.checksum_sha256 or "NOT AVAILABLE"
        size = str(artifact.size_bytes) if artifact.size_bytes is not None else "NOT AVAILABLE"
        lines.append(
            "| "
            f"{_md_cell(artifact.role.value)} | {_md_cell(artifact.artifact_id)} | "
            f"{_md_cell(artifact.artifact_type)} | {_md_cell(artifact.relative_path)} | "
            f"{_md_cell(checksum)} | {_md_cell(size)} | "
            f"{'yes — synthetic only' if artifact.placeholder else 'no'} |"
        )
    lines.extend(
        [
            "",
            "## Cluster records",
            "",
            "Connectivity definition (explicit, with no inferred default): "
            f"{_md_cell(manifest.cluster_connectivity_definition)}",
            "",
            "| Cluster | Source map | Extent (voxels) | Peak statistic | "
            "Peak coordinate (mm) | Space |",
            "| --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    if clusters:
        for cluster in clusters:
            coordinate = ", ".join(f"{value:g}" for value in cluster.peak_coordinate_mm)
            lines.append(
                "| "
                f"{_md_cell(cluster.cluster_id)} | "
                f"{_md_cell(cluster.source_map_artifact_id)} | "
                f"{cluster.extent_voxels} | {cluster.peak_statistic:g} | "
                f"{_md_cell(coordinate)} | {_md_cell(cluster.coordinate_space)} |"
            )
    else:
        lines.append("| None recorded | — | — | — | — | — |")
    lines.extend(
        [
            "",
            "## Interpretation boundaries",
            "",
            "- Effect-map semantics are not inferred; use the registered method and "
            "version evidence.",
            "- Cluster connectivity has no system default and is reproduced exactly "
            "as declared above.",
            f"- {RESEARCH_USE_NOTICE}",
            "",
        ]
    )
    return "\n".join(lines)


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
