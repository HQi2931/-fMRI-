"""Layered structural, compatibility and scientific Skill validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from neuroagent.domain.fmri.metrics import (
    MetricKind,
    SmoothingTiming,
    TemporalFilterTiming,
    validate_alff_falff_input,
    validate_frequency_resolution,
    validate_reho_input,
)
from neuroagent.domain.fmri.preprocessing import ScrubbingMethod
from neuroagent.skills.loader import SkillLoader, SkillLoadError
from neuroagent.skills.models import (
    IssueSeverity,
    SkillResolution,
    SkillSpec,
    SkillValidationIssue,
    ValidationReport,
)


class SkillValidator:
    def __init__(self, schema_root: Path | None = None) -> None:
        self._schema_root = schema_root or Path(__file__).resolve().parents[2] / "skills"
        self._loader = SkillLoader()

    def validate_spec(self, spec: SkillSpec) -> ValidationReport:
        issues: list[SkillValidationIssue] = []
        if spec.status.value == "reviewed" and not spec.evidence_refs:
            issues.append(
                SkillValidationIssue(
                    code="REVIEWED_SKILL_WITHOUT_EVIDENCE",
                    severity=IssueSeverity.BLOCKING,
                    message="Reviewed Skills require source or method evidence",
                    path="evidence_refs",
                )
            )
        requires_method_review = spec.skill_id.startswith(
            ("rsfmri.preprocess.", "rsfmri.metric.", "rsfmri.pipeline.", "rsfmri.statistics.")
        )
        if (
            spec.status.value == "reviewed"
            and requires_method_review
            and "fmri_methodologist" not in spec.reviewed_by
        ):
            issues.append(
                SkillValidationIssue(
                    code="SCIENTIFIC_SKILL_WITHOUT_METHODOLOGIST_REVIEW",
                    severity=IssueSeverity.BLOCKING,
                    message="Reviewed scientific Skills require fmri_methodologist review",
                    path="reviewed_by",
                )
            )
        return ValidationReport(issues=tuple(issues))

    def validate_parameter_payload(self, skill_directory: str, payload: Any) -> ValidationReport:
        """Validate any of the six checked-in Skill parameter contracts at runtime."""

        schema = self._schema_root / skill_directory / "parameters.schema.json"
        try:
            self._loader.validate_parameters(payload, schema)
        except (OSError, ValueError, SkillLoadError) as exc:
            return ValidationReport(
                issues=(
                    SkillValidationIssue(
                        code="PARAMETER_SCHEMA_VALIDATION_FAILED",
                        severity=IssueSeverity.BLOCKING,
                        message=str(exc),
                        path=skill_directory,
                    ),
                )
            )
        return ValidationReport()

    def validate_resolution(self, resolution: SkillResolution) -> ValidationReport:
        issues = list(resolution.issues)
        request = resolution.request
        for spec in resolution.selected_specs:
            issues.extend(self.validate_spec(spec).issues)
            checks = (
                (
                    "MATLAB_VERSION_INCOMPATIBLE",
                    "matlab_version",
                    spec.compatibility.matlab,
                    resolution.environment.matlab_version,
                ),
                (
                    "SPM_VERSION_INCOMPATIBLE",
                    "spm_version",
                    spec.compatibility.spm,
                    resolution.environment.spm_version,
                ),
                (
                    "DPABI_VERSION_INCOMPATIBLE",
                    "dpabi_version",
                    spec.compatibility.dpabi,
                    resolution.environment.dpabi_version,
                ),
                (
                    "ADAPTER_VERSION_INCOMPATIBLE",
                    "adapter_version",
                    spec.compatibility.adapter,
                    resolution.environment.adapter_version,
                ),
            )
            for code, field, required, actual in checks:
                if _compatible(required, actual):
                    continue
                issues.append(
                    SkillValidationIssue(
                        code=code,
                        severity=IssueSeverity.BLOCKING,
                        message=(
                            f"Skill {spec.skill_id}@{spec.version} targets {required}, not {actual}"
                        ),
                        path=f"environment.{field}",
                    )
                )
        requested = set(request.requested_metrics)
        if request.request_preprocessing and request.preprocessing is not None:
            issues.extend(
                self.validate_parameter_payload(
                    "plan-dpabi-preprocessing",
                    {
                        "input_manifest_hash": request.input_manifest_hash,
                        "base_cfg_artifact_id": request.base_cfg_artifact_id,
                        "request_preprocessing": True,
                        "preprocessing": request.preprocessing.model_dump(mode="json"),
                    },
                ).issues
            )
        if request.alff_falff is not None:
            issues.extend(
                self.validate_parameter_payload(
                    "plan-alff-falff", request.alff_falff.model_dump(mode="json")
                ).issues
            )
        if request.reho is not None:
            issues.extend(
                self.validate_parameter_payload(
                    "plan-reho", request.reho.model_dump(mode="json")
                ).issues
            )
        if requested and request.request_preprocessing:
            issues.extend(_validate_preprocessing_checkpoint(request))
        else:
            if requested & {MetricKind.ALFF, MetricKind.FALFF} and request.alff_falff:
                for code in validate_alff_falff_input(request.input_artifact, request.alff_falff):
                    issues.append(_scientific_issue(code))
            if MetricKind.REHO in requested and request.reho:
                for code in validate_reho_input(request.input_artifact, request.reho):
                    issues.append(_scientific_issue(code))

        preprocessing = request.preprocessing
        if preprocessing is not None and requested:
            if preprocessing.smoothing.timing is not SmoothingTiming.DISABLED:
                issues.append(_scientific_issue("METRIC_PREPROCESSING_SMOOTHING_CONFLICT"))
            if request.alff_falff is not None:
                if preprocessing.temporal_filter.timing is not (request.alff_falff.filter_timing):
                    issues.append(_scientific_issue("ALFF_FILTER_PLAN_MISMATCH"))
                if (
                    MetricKind.FALFF in requested
                    and preprocessing.temporal_filter.timing
                    is TemporalFilterTiming.BEFORE_NORMALIZE
                ):
                    issues.append(_scientific_issue("FALFF_PREPROCESSING_FILTERED_INPUT"))
            if request.reho is not None:
                planned_band = preprocessing.temporal_filter.frequency_band
                if planned_band != request.reho.temporal_filter_band:
                    issues.append(_scientific_issue("REHO_PREPROCESSING_FILTER_MISMATCH"))
                if (
                    preprocessing.temporal_filter.add_mean_back
                    != request.reho.temporal_filter_add_mean_back
                ):
                    issues.append(_scientific_issue("REHO_ADD_MEAN_BACK_MISMATCH"))
        return ValidationReport(issues=tuple(issues))


def _compatible(required: str, actual: str) -> bool:
    if required.endswith(".*"):
        return actual.startswith(required[:-1])
    return required == actual


_MESSAGES = {
    "INPUT_METADATA_UNVERIFIED": (
        "Metric planning requires executor-verified headers and immutable metadata evidence"
    ),
    "TR_LINEAGE_MISMATCH": "Metric TR differs from the executor-verified Artifact TR",
    "EFFECTIVE_VOLUME_COUNT_UNKNOWN": (
        "The actual number of retained volumes is unknown; CUT scrubbing requires a later "
        "verified Artifact before metric planning"
    ),
    "FREQUENCY_BAND_BELOW_EFFECTIVE_RESOLUTION": (
        "The requested frequency band is below the resolution supported by retained volumes"
    ),
    "FALFF_INPUT_ALREADY_FILTERED": "Standard fALFF must consume an unfiltered time series",
    "ALFF_INPUT_SPATIALLY_SMOOTHED": "ALFF/fALFF input smoothing is not approved by this Skill",
    "ALFF_FILTER_TIMING_BEFORE_NORMALIZE": (
        "DPABI BeforeNormalize filtering would make ALFF/fALFF consume filtered input"
    ),
    "REHO_INPUT_SPATIALLY_SMOOTHED": "ReHo must be calculated before spatial smoothing",
    "REHO_INPUT_ALREADY_FILTERED": (
        "The reviewed ReHo DAG requires an unfiltered checkpoint and owns any requested filter"
    ),
    "MASK_GRID_MISMATCH": "Mask and functional-image grid signatures do not match",
    "MASK_ARTIFACT_MISMATCH": (
        "All metrics in one workflow must use the same frozen brain-mask Artifact"
    ),
    "METRIC_PREPROCESSING_SMOOTHING_CONFLICT": (
        "Metric Skills require the common preprocessing checkpoint to remain unsmoothed"
    ),
    "ALFF_FILTER_PLAN_MISMATCH": (
        "Common preprocessing and ALFF/fALFF declare different temporal-filter timing"
    ),
    "FALFF_PREPROCESSING_FILTERED_INPUT": (
        "Standard fALFF cannot consume a BeforeNormalize-filtered common checkpoint"
    ),
    "REHO_PREPROCESSING_FILTER_MISMATCH": (
        "Common preprocessing and ReHo declare different temporal-filter bands"
    ),
    "REHO_PREPROCESSING_PREFILTERED_INPUT": (
        "BeforeNormalize filtering cannot produce the unfiltered normalized checkpoint required "
        "by the reviewed ReHo DAG"
    ),
    "REHO_ADD_MEAN_BACK_MISMATCH": (
        "Common preprocessing and ReHo declare different add-mean-back filter semantics"
    ),
}


def _validate_preprocessing_checkpoint(request: Any) -> list[SkillValidationIssue]:
    """Validate a same-DAG expected checkpoint without pretending it is verified.

    Actual TR, retained-volume count, grid and mask compatibility remain a
    blocking executor-side header check.  Here we only validate the frozen
    expectations that can safely be known before the run.
    """

    preprocessing = request.preprocessing
    if preprocessing is None:
        return []
    issues: list[SkillValidationIssue] = []
    requested = set(request.requested_metrics)
    expected_count = preprocessing.expected_time_points
    if expected_count is None:
        issues.append(_scientific_issue("EFFECTIVE_VOLUME_COUNT_UNKNOWN"))
        return issues
    expected_count -= preprocessing.dummy_scans

    alff = request.alff_falff
    if requested & {MetricKind.ALFF, MetricKind.FALFF} and alff is not None:
        if alff.tr_seconds != preprocessing.tr_seconds:
            issues.append(_scientific_issue("TR_LINEAGE_MISMATCH"))
        for code in validate_frequency_resolution(
            tr_seconds=preprocessing.tr_seconds,
            volume_count=expected_count,
            bands=(alff.frequency_band,),
        ):
            issues.append(_scientific_issue(code))
        if alff.filter_timing is TemporalFilterTiming.BEFORE_NORMALIZE:
            issues.append(_scientific_issue("ALFF_FILTER_TIMING_BEFORE_NORMALIZE"))

    reho = request.reho
    if MetricKind.REHO in requested and reho is not None:
        if reho.tr_seconds != preprocessing.tr_seconds:
            issues.append(_scientific_issue("TR_LINEAGE_MISMATCH"))
        if preprocessing.temporal_filter.timing is TemporalFilterTiming.BEFORE_NORMALIZE:
            issues.append(_scientific_issue("REHO_PREPROCESSING_PREFILTERED_INPUT"))
        if (
            preprocessing.scrubbing.enabled
            and preprocessing.scrubbing.method is ScrubbingMethod.CUT
        ):
            issues.append(_scientific_issue("EFFECTIVE_VOLUME_COUNT_UNKNOWN"))
        else:
            for code in validate_frequency_resolution(
                tr_seconds=preprocessing.tr_seconds,
                volume_count=expected_count,
                bands=(reho.temporal_filter_band,),
            ):
                issues.append(_scientific_issue(code))

    mask_ids = {
        item
        for item in (
            alff.mask_artifact_id if alff is not None else None,
            reho.mask_artifact_id if reho is not None else None,
        )
        if item is not None
    }
    if len(mask_ids) > 1:
        issues.append(_scientific_issue("MASK_ARTIFACT_MISMATCH"))
    return issues


def _scientific_issue(code: str) -> SkillValidationIssue:
    return SkillValidationIssue(
        code=code,
        severity=IssueSeverity.BLOCKING,
        message=_MESSAGES[code],
        path="input_artifact",
        evidence_ref="docs/architecture/fmri-skill-layer.md#7-alfffalff-and-reho",
        remediation=(
            "Create a new plan with a compatible typed artifact or explicit protocol choice"
        ),
    )
