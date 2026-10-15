"""Contracts for externally produced subject-level metric maps.

The scanner records byte and image-grid evidence, while filename and directory
identities remain candidates. Processing history and anatomical space stay
unknown unless a researcher declares them; inclusion never creates verified
execution provenance.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, model_validator

from neuroagent.domain.fmri.artifacts import FrozenModel, MetricScaling
from neuroagent.domain.fmri.metrics import MetricKind

ExternalCoordinateSpace = Literal["MNI", "Talairach", "unknown"]
ExternalMetricLayout = Literal["grouped", "paired_conditions"]
ExternalMappingEvidence = Literal["unknown", "candidate"]


def external_metric_artifact_id(
    import_id: str, subject_id: str, condition: str | None = None
) -> str:
    """Stable opaque reference for one reviewed external metric image."""

    identity = f"{import_id}\0{subject_id}" + (f"\0{condition}" if condition is not None else "")
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"external-metric-{digest}"


def external_mask_artifact_id(import_id: str, mask_sha256: str) -> str:
    """Stable opaque reference for the exact mask frozen in an inclusion."""

    digest = hashlib.sha256(f"{import_id}\0{mask_sha256}".encode()).hexdigest()
    return f"external-mask-{digest}"


def external_analysis_manifest_hash(
    import_id: str, source_tree_hash: str, inclusion_hash: str
) -> str:
    payload = {
        "external_import_id": import_id,
        "source_tree_hash": source_tree_hash,
        "inclusion_hash": inclusion_hash,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def external_identity_mapping_hash(
    layout: ExternalMetricLayout, images: tuple[ExternalMetricImage, ...]
) -> str:
    """Hash the scanner's subject/group/condition mapping separately from file bytes."""

    mapping = [
        {
            "relative_path": image.relative_path,
            "subject_id": image.subject_id,
            "subject_id_source": image.subject_id_source,
            "subject_id_evidence": image.subject_id_evidence,
            "group": image.group,
            "group_source": image.group_source,
            "group_evidence": image.group_evidence,
            "condition": image.condition,
            "condition_source": image.condition_source,
            "condition_evidence": image.condition_evidence,
            "session_id": image.session_id,
            "session_evidence": image.session_evidence,
            "run_id": image.run_id,
            "run_evidence": image.run_evidence,
        }
        for image in sorted(
            images, key=lambda item: (item.relative_path.casefold(), item.relative_path)
        )
    ]
    payload = {"layout": layout, "images": mapping}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


class ExternalMetricImage(FrozenModel):
    """One hashed, spatially inspected input image within an external import."""

    subject_id: str = Field(min_length=1)
    group: str = Field(min_length=1)
    condition: str | None = Field(default=None, min_length=1)
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(gt=0)
    shape_ijk: tuple[int, int, int]
    affine: tuple[tuple[float, float, float, float], ...]
    grid_signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    metric_kind: MetricKind
    scaling: MetricScaling
    # Import scanning can only propose identities based on source labels. These
    # fields deliberately remain candidates; external data never gains
    # execution-verified identity evidence merely by being included.
    subject_id_source: Literal["unknown", "filename"] = "unknown"
    subject_id_evidence: ExternalMappingEvidence = "unknown"
    group_source: Literal["unknown", "first_level_directory"] = "unknown"
    group_evidence: ExternalMappingEvidence = "unknown"
    condition_source: Literal["unknown", "first_level_directory"] = "unknown"
    condition_evidence: ExternalMappingEvidence = "unknown"
    session_id: None = None
    session_evidence: Literal["unknown"] = "unknown"
    run_id: None = None
    run_evidence: Literal["unknown"] = "unknown"


class ExternalMetricInspection(FrozenModel):
    """Immutable scan result. It is not analysis approval or QC."""

    schema_version: Literal["1.0"] = "1.0"
    layout: ExternalMetricLayout = "grouped"
    source_tree_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_mapping_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    metric_kind: MetricKind | None = None
    scaling: MetricScaling | None = None
    images: tuple[ExternalMetricImage, ...]
    group_order: tuple[str, ...]
    grid_signature: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    coordinate_space: ExternalCoordinateSpace = "unknown"
    blocking_issues: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    unknown_processing_fields: tuple[str, ...] = (
        "TR and frequency band",
        "temporal filtering and nuisance regression",
        "smoothing and scrubbing",
        "mask source and version",
        "preprocessing software and version",
    )


class ExternalMetricInclusion(FrozenModel):
    """Researcher decision that permits an inspected import to be selected."""

    included_subject_ids: tuple[str, ...]
    excluded_subject_ids: tuple[str, ...]
    group_order: tuple[str, ...]
    coordinate_space: ExternalCoordinateSpace
    coordinate_space_source: Literal["user_confirmed", "unknown"]
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)
    unknown_processing_acknowledged: bool
    # A researcher declaration is separate from the scanner's candidate
    # mapping and is not equivalent to executor-verified provenance.
    identity_mapping_evidence: Literal["unknown", "declared"] = "unknown"
    identity_mapping_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    mask_relative_path: str | None = Field(default=None, min_length=1, max_length=4096)
    mask_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    mask_size_bytes: int | None = Field(default=None, gt=0)
    mask_grid_signature: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def mask_evidence_is_complete(self) -> ExternalMetricInclusion:
        mask_fields = (
            self.mask_relative_path,
            self.mask_sha256,
            self.mask_size_bytes,
            self.mask_grid_signature,
        )
        if any(value is not None for value in mask_fields) and any(
            value is None for value in mask_fields
        ):
            raise ValueError("external mask path, hash, size, and grid must be recorded together")
        if self.mask_relative_path is not None:
            path = PurePosixPath(self.mask_relative_path)
            if path.is_absolute() or ".." in path.parts or "\\" in self.mask_relative_path:
                raise ValueError("external mask path must remain relative to the import root")
        return self
