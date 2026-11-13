"""Frozen manifest projections consumed by execution compilers."""

from __future__ import annotations

from typing import Any, Protocol

from neuroagent.domain.fmri.dataset import DatasetKind


class ManifestSubject(Protocol):
    subject_id: str
    session_id: str | None
    functional_files: list[str]
    anatomical_files: list[str]

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]: ...


class ManifestDatasetProfile(Protocol):
    kind: DatasetKind

    def model_dump(self, *, mode: str = "python") -> dict[str, Any]: ...


class FrozenManifest(Protocol):
    manifest_id: str
    dataset_id: str
    content_hash: str
    profile: ManifestDatasetProfile
    subjects: list[ManifestSubject]
    selected_input_stage: str | None
