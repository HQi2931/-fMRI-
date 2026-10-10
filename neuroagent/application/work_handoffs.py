"""Server-derived preprocessing evidence shared with the analysis role."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from neuroagent.application.artifact_content import artifact_content_path
from neuroagent.application.errors import ApplicationError, ConflictError, InputValidationError
from neuroagent.application.hashing import content_hash
from neuroagent.application.ports import RepositoryPort
from neuroagent.application.statistics_draft import DesignIssue, QcBinding
from neuroagent.domain.fmri.artifacts import ArtifactKind, ArtifactLineage


class HandoffArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    artifact_type: str
    checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    relative_path: str
    metric_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    lineage: ArtifactLineage
    lineage_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    provenance: dict[str, Any]
    provenance_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class WorkAnalysisHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_agent: Literal["preprocessing"] = "preprocessing"
    to_agent: Literal["analysis"] = "analysis"
    handoff_id: str
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["ready", "received", "blocked", "stale"]
    data_references: dict[str, Any]
    blocking_items: tuple[str, ...] = ()


class WorkPreprocessingHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    from_agent: Literal["analysis"] = "analysis"
    to_agent: Literal["preprocessing"] = "preprocessing"
    handoff_id: str
    evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["pending", "received", "blocked", "stale"] = "pending"
    reason: str = Field(min_length=1, max_length=1000)
    required_outputs: tuple[str, ...] = Field(min_length=1, max_length=9)
    data_references: dict[str, Any]
    blocking_items: tuple[str, ...] = ()


def preprocessing_output_requirements(outputs: tuple[str, ...]) -> tuple[str, ...]:
    allowed = {
        value.casefold(): value
        for value in (
            "ALFF",
            "mALFF",
            "zALFF",
            "fALFF",
            "mfALFF",
            "zfALFF",
            "ReHo",
            "mReHo",
            "zReHo",
        )
    }
    if not outputs or any(value.strip().casefold() not in allowed for value in outputs):
        raise InputValidationError(
            "work_handoff_outputs_invalid", "Specify registered ALFF/fALFF/ReHo output names."
        )
    return tuple(sorted({allowed[value.strip().casefold()] for value in outputs}))


def preprocessing_handoff_hash(references: dict[str, Any], outputs: tuple[str, ...]) -> str:
    return content_hash({"data_references": references, "required_outputs": list(outputs)})


def require_same_preprocessing_handoff(
    frozen: WorkPreprocessingHandoff, current: WorkPreprocessingHandoff
) -> WorkPreprocessingHandoff:
    if (
        frozen.status not in {"pending", "received"}
        or current.status != "pending"
        or frozen.evidence_hash
        != preprocessing_handoff_hash(frozen.data_references, frozen.required_outputs)
        or frozen.handoff_id != frozen.evidence_hash[:24]
        or frozen.evidence_hash != current.evidence_hash
    ):
        raise ConflictError(
            "work_handoff_stale", "Upstream metric requirements or evidence changed."
        )
    return current.model_copy(update={"status": "received"})


SourceBinding = Callable[
    [str], tuple[QcBinding, tuple[str, ...], frozenset[str] | None, str, DesignIssue | None]
]


def prepare_analysis_handoff(
    repository: RepositoryPort,
    source_binding: SourceBinding,
    *,
    run_id: str,
    project_id: str,
    verify_files: bool = True,
) -> WorkAnalysisHandoff:
    """Read authoritative evidence; never manufacture trusted model metadata."""

    run = repository.get_run(run_id)
    if run.project_id != project_id:
        raise ConflictError(
            "work_handoff_project_mismatch", "Source run belongs to another project."
        )
    plan = repository.get_plan(run.plan_revision_id)
    references: dict[str, Any] = {
        "project_id": project_id,
        "source_run_id": run_id,
        "source_run_state": run.state.value,
        "source_attempt": run.attempt,
        "source_plan_revision_id": plan.plan_revision_id,
        "source_plan_hash": plan.plan_hash,
        "input_manifest_hash": plan.manifest_hash,
    }
    blockers: list[str] = []
    if run.state.value != "succeeded":
        blockers.append("source_run_not_succeeded")
    if plan.plan.get("kind") == "statistical_design":
        blockers.append("source_is_statistical_run")
    try:
        manifest_id = plan.plan.get("dataset_manifest_id")
        if not isinstance(manifest_id, str) or not manifest_id:
            raise ConflictError("source_manifest_unavailable", "Source manifest is missing.")
        manifest = repository.get_manifest(manifest_id)
        dataset = repository.get_dataset(manifest.dataset_id)
        references.update(
            dataset_id=dataset.dataset_id,
            dataset_manifest_id=manifest_id,
            dataset_manifest_hash=manifest.content_hash,
        )
        if dataset.project_id != project_id or manifest.content_hash != plan.manifest_hash:
            blockers.append("source_manifest_mismatch")
        if dataset.current_manifest_id != manifest_id:
            blockers.append("source_manifest_superseded")
        binding, included, allowed_ids, qc_manifest_hash, issue = source_binding(run_id)
        references.update(
            qc_decision=binding.model_dump(mode="json"),
            included_subject_ids=list(included),
            qc_manifest_hash=qc_manifest_hash,
        )
        if issue is not None:
            blockers.append(issue.code)
        elif qc_manifest_hash != plan.manifest_hash:
            blockers.append("qc_manifest_hash_mismatch")
        artifacts = repository.list_artifacts(run_id)
        metrics: list[HandoffArtifact] = []
        masks: dict[str, HandoffArtifact] = {}
        project = repository.get_project(project_id)
        for artifact in artifacts:
            raw = artifact.provenance.get("lineage")
            if not isinstance(raw, dict):
                continue
            lineage = ArtifactLineage.model_validate(raw)
            if lineage.kind not in {
                ArtifactKind.ALFF_MAP,
                ArtifactKind.FALFF_MAP,
                ArtifactKind.REHO_MAP,
                ArtifactKind.BRAIN_MASK,
            }:
                continue
            if lineage.kind is not ArtifactKind.BRAIN_MASK and (
                lineage.subject_id not in included
                or (allowed_ids is not None and artifact.artifact_id not in allowed_ids)
            ):
                continue
            if (
                artifact.project_id != project_id
                or lineage.artifact_id != artifact.artifact_id
                or not lineage.metadata_verified
                or lineage.subject_manifest_hash != plan.manifest_hash
            ):
                blockers.append("handoff_artifact_lineage_mismatch")
                continue
            evidence = HandoffArtifact(
                artifact_id=artifact.artifact_id,
                artifact_type=artifact.artifact_type,
                checksum=artifact.checksum,
                size_bytes=artifact.size_bytes,
                relative_path=artifact.relative_path,
                metric_version=lineage.producer_step_hash,
                lineage=lineage,
                lineage_hash=content_hash(lineage.model_dump(mode="json")),
                provenance=artifact.provenance,
                provenance_hash=content_hash(artifact.provenance),
            )
            if lineage.kind is ArtifactKind.BRAIN_MASK:
                masks[artifact.artifact_id] = evidence
            else:
                metrics.append(evidence)
        used_masks: dict[str, HandoffArtifact] = {}
        for metric in metrics:
            mask = masks.get(metric.lineage.mask_artifact_id or "")
            if (
                mask is None
                or mask.lineage.subject_id is not None
                or mask.lineage.space != metric.lineage.space
                or mask.lineage.grid_signature != metric.lineage.grid_signature
            ):
                blockers.append("handoff_mask_mismatch")
            elif mask is not None:
                used_masks[mask.artifact_id] = mask
                shared = mask.provenance.get("shared_effective_mask")
                if shared is not None:
                    if (
                        not isinstance(shared, dict)
                        or shared.get("method") != "identical_effective_masks"
                    ):
                        blockers.append("handoff_mask_evidence_invalid")
                        continue
                    source_rows = shared.get("source_masks")
                    if not isinstance(source_rows, list) or not source_rows:
                        blockers.append("handoff_mask_evidence_invalid")
                        continue
                    for row in source_rows:
                        source = next(
                            (
                                item
                                for item in masks.values()
                                if isinstance(row, dict)
                                and item.relative_path == row.get("relative_path")
                            ),
                            None,
                        )
                        if (
                            source is None
                            or source.lineage.subject_id != row.get("subject_id")
                            or source.checksum != row.get("checksum")
                            or source.size_bytes != row.get("size_bytes")
                            or source.checksum != mask.checksum
                            or source.lineage.space != mask.lineage.space
                            or source.lineage.grid_signature != mask.lineage.grid_signature
                        ):
                            blockers.append("handoff_mask_evidence_invalid")
                        else:
                            used_masks[source.artifact_id] = source
                    if metric.lineage.subject_id not in {
                        row.get("subject_id") for row in source_rows if isinstance(row, dict)
                    }:
                        blockers.append("handoff_mask_evidence_invalid")
        # Preserve QC order independently of artifact registration/file order.
        positions = {subject: index for index, subject in enumerate(included)}
        metrics.sort(
            key=lambda item: (
                positions[item.lineage.subject_id or ""],
                item.artifact_type,
                item.lineage.condition or "",
                item.artifact_id,
            )
        )
        references["metrics"] = [item.model_dump(mode="json") for item in metrics]
        references["masks"] = [
            used_masks[key].model_dump(mode="json") for key in sorted(used_masks)
        ]
        if not metrics or {item.lineage.subject_id for item in metrics} != set(included):
            blockers.append("handoff_metric_subjects_missing")
        identities = [
            (item.artifact_type, item.lineage.subject_id, item.lineage.condition)
            for item in metrics
        ]
        if len(identities) != len(set(identities)):
            blockers.append("handoff_duplicate_metric")
        versions: dict[tuple[str, str | None], set[str]] = {}
        for item in metrics:
            versions.setdefault((item.artifact_type, item.lineage.condition), set()).add(
                item.metric_version
            )
        if any(len(values) != 1 for values in versions.values()):
            blockers.append("handoff_metric_versions_mixed")
        if verify_files and not blockers:
            for item in (*metrics, *used_masks.values()):
                artifact_content_path(repository.get_artifact(item.artifact_id), run, project)
    except ApplicationError as exc:
        blockers.append(exc.code)
    except ValueError:
        blockers.append("handoff_evidence_invalid")
    evidence_hash = content_hash(references)
    return WorkAnalysisHandoff(
        handoff_id=evidence_hash[:24],
        evidence_hash=evidence_hash,
        status="blocked" if blockers else "ready",
        data_references=references,
        blocking_items=tuple(dict.fromkeys(blockers)),
    )


def require_same_handoff(
    frozen: WorkAnalysisHandoff, current: WorkAnalysisHandoff
) -> WorkAnalysisHandoff:
    if (
        frozen.status not in {"ready", "received"}
        or current.status != "ready"
        or frozen.evidence_hash != content_hash(frozen.data_references)
        or frozen.handoff_id != frozen.evidence_hash[:24]
        or frozen.evidence_hash != current.evidence_hash
    ):
        raise ConflictError(
            "work_handoff_stale",
            "Preprocessing handoff changed or is not ready.",
            blocking_items=list(current.blocking_items),
        )
    return current.model_copy(update={"status": "received"})


def handoff_reference_summary(references: dict[str, Any]) -> dict[str, Any]:
    """Bounded model facts; full image/subject provenance stays on the server."""
    result = {
        key: references.get(key)
        for key in (
            "project_id",
            "source_run_id",
            "source_run_state",
            "source_plan_revision_id",
            "source_plan_hash",
            "dataset_id",
            "dataset_manifest_id",
            "input_manifest_hash",
        )
    }
    metrics = references.get("metrics", [])
    result["metric_count"] = len(metrics) if isinstance(metrics, list) else 0
    result["metric_types"] = (
        sorted(
            {
                str(item["artifact_type"])
                for item in metrics
                if isinstance(item, dict) and "artifact_type" in item
            }
        )
        if isinstance(metrics, list)
        else []
    )
    subjects = references.get("included_subject_ids", [])
    result["subject_count"] = len(subjects) if isinstance(subjects, list) else 0
    return result
