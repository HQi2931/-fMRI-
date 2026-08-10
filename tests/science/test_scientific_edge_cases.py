from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from neuroagent.domain.fmri import (
    AlffFalffParameters,
    AnalysisImage,
    ArtifactLineage,
    Centering,
    CovariateColumn,
    CovariateValue,
    FrequencyBand,
    MetricKind,
    MissingValuePolicy,
    QcCheck,
    QcReviewRevision,
    QcSeverity,
    RehoParameters,
    StatisticalDesignRevision,
    StatisticalTest,
    Tail,
    TemporalFilterTiming,
    validate_alff_falff_input,
    validate_reho_input,
)
from neuroagent.domain.fmri.qc import assert_statistics_ready
from tests.science.conftest import ALFF_PROVENANCE, provenance
from tests.science.test_metric_rules import make_alff, make_reho
from tests.science.test_statistics import one_sample


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"temporally_filtered": True}, "same state"),
        ({"spatially_smoothed": True}, "same state"),
        ({"voxel_size_mm": (0.0, 3.0, 3.0)}, "positive"),
        (
            {"spatially_smoothed": True, "smoothing_fwhm_mm": (0.0, 6.0, 6.0)},
            "positive",
        ),
        ({"mask_artifact_id": None, "mask_grid_signature": "grid-3mm"}, "requires"),
    ],
)
def test_artifact_lineage_rejects_inconsistent_metadata(
    base_lineage: ArtifactLineage, updates: dict[str, object], message: str
) -> None:
    values = base_lineage.model_dump()
    values.update(updates)
    with pytest.raises(ValidationError, match=message):
        ArtifactLineage.model_validate(values)


def test_lineage_metadata_verification_fails_closed(base_lineage: ArtifactLineage) -> None:
    unverified = base_lineage.model_dump(mode="python")
    unverified.update(
        metadata_verified=False,
        tr_seconds=None,
        volume_count=None,
        metadata_evidence_hash=None,
    )
    lineage = ArtifactLineage.model_validate(unverified)
    assert lineage.metadata_verified is False

    missing_evidence = base_lineage.model_dump(mode="python")
    missing_evidence["metadata_evidence_hash"] = None
    with pytest.raises(ValidationError, match="evidence hash"):
        ArtifactLineage.model_validate(missing_evidence)

    forged_values = unverified.copy()
    forged_values["tr_seconds"] = 2.0
    with pytest.raises(ValidationError, match="unverified lineage"):
        ArtifactLineage.model_validate(forged_values)


def test_frequency_band_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="low_hz < high_hz"):
        FrequencyBand(low_hz=0.08, high_hz=0.01)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"requested_metrics": ()}, "select"),
        ({"requested_metrics": (MetricKind.ALFF, MetricKind.ALFF)}, "duplicates"),
        ({"requested_scalings": ()}, "at least one"),
        (
            {"result_smoothing": True, "result_smoothing_fwhm_mm": None},
            "specified together",
        ),
        ({"provenance": provenance(ALFF_PROVENANCE[:-1])}, "provenance missing"),
        (
            {
                "provenance": (
                    *provenance(ALFF_PROVENANCE),
                    provenance((ALFF_PROVENANCE[0],))[0],
                )
            },
            "unique",
        ),
    ],
)
def test_alff_parameter_contract_rejects_ambiguous_choices(
    updates: dict[str, object], message: str
) -> None:
    values = make_alff().model_dump()
    values.update(updates)
    with pytest.raises(ValidationError, match=message):
        AlffFalffParameters.model_validate(values)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"temporal_filter_band": {"low_hz": 0.01, "high_hz": 0.3}}, "Nyquist"),
        ({"requested_scalings": ()}, "at least one"),
        ({"mask_artifact_id": None}, "require a mask"),
        ({"smooth_reho": True, "smooth_reho_fwhm_mm": None}, "specified together"),
        (
            {"global_result_smoothing": True, "global_result_smoothing_fwhm_mm": None},
            "specified together",
        ),
    ],
)
def test_reho_parameter_contract_rejects_ambiguous_choices(
    updates: dict[str, object], message: str
) -> None:
    values = make_reho().model_dump()
    values.update(updates)
    with pytest.raises(ValidationError, match=message):
        RehoParameters.model_validate(values)


def test_metric_lineage_reports_mask_filter_and_timing_conflicts(
    base_lineage: ArtifactLineage,
) -> None:
    alff_values = make_alff().model_dump()
    alff_values["filter_timing"] = TemporalFilterTiming.BEFORE_NORMALIZE
    alff = AlffFalffParameters.model_validate(alff_values)
    different_mask = base_lineage.model_copy(update={"mask_artifact_id": "mask-other"})
    assert set(validate_alff_falff_input(different_mask, alff)) == {
        "MASK_GRID_MISMATCH",
        "ALFF_FILTER_TIMING_BEFORE_NORMALIZE",
    }

    filtered = base_lineage.model_copy(
        update={
            "temporally_filtered": True,
            "frequency_band": FrequencyBand(low_hz=0.02, high_hz=0.09),
        }
    )
    assert validate_reho_input(filtered, make_reho()) == ("REHO_INPUT_ALREADY_FILTERED",)
    reho_values = make_reho().model_dump()
    reho_values["temporal_filter_band"] = None
    reho_values["temporal_filter_add_mean_back"] = None
    unfiltered_protocol = RehoParameters.model_validate(reho_values)
    assert validate_reho_input(filtered, unfiltered_protocol) == ("REHO_INPUT_ALREADY_FILTERED",)


def valid_qc_data() -> dict[str, object]:
    return {
        "review_revision_id": "qc-1",
        "input_manifest_hash": "a" * 64,
        "metric_artifact_ids": ("image-1",),
        "checks": (
            QcCheck(
                code="CHECK",
                severity=QcSeverity.BLOCKING,
                passed=True,
                evidence_artifact_ids=("evidence",),
                message="passed",
            ),
        ),
        "included_subject_ids": ("sub-01",),
        "excluded_subject_ids": ("sub-02",),
        "exclusion_reasons": (("sub-02", "motion"),),
        "approved": True,
        "approved_by": "reviewer",
        "approval_reason": "reviewed",
        "content_hash": "c" * 64,
    }


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"included_subject_ids": ("sub-01", "sub-01")}, "included"),
        ({"excluded_subject_ids": ("sub-02", "sub-02")}, "excluded"),
        ({"included_subject_ids": ("sub-01", "sub-02")}, "both"),
        ({"exclusion_reasons": ()}, "every excluded"),
        (
            {"exclusion_reasons": (("sub-02", "a"), ("sub-02", "b"))},
            "unique",
        ),
        (
            {
                "checks": (
                    QcCheck(
                        code="FAIL",
                        severity=QcSeverity.BLOCKING,
                        passed=False,
                        evidence_artifact_ids=("e",),
                        message="failed",
                    ),
                )
            },
            "blocking failures",
        ),
        ({"approved_by": None}, "approver"),
        (
            {"approved": False, "approved_by": "reviewer", "approval_reason": "reason"},
            "must not carry",
        ),
    ],
)
def test_qc_revision_rejects_inauditable_state(updates: dict[str, object], message: str) -> None:
    values = valid_qc_data()
    values.update(updates)
    with pytest.raises(ValidationError, match=message):
        QcReviewRevision.model_validate(values)


def test_unapproved_qc_cannot_enter_statistics() -> None:
    values = valid_qc_data()
    values.update(approved=False, approved_by=None, approval_reason=None)
    review = QcReviewRevision.model_validate(values)
    with pytest.raises(ValueError, match="approved QC"):
        assert_statistics_ready(review, ("sub-01",))


def test_statistical_values_must_be_finite() -> None:
    with pytest.raises(ValidationError, match="finite"):
        CovariateValue(subject_id="sub-01", value=math.nan)
    values = one_sample().model_dump()
    values["contrast"] = (math.inf, 0.0)
    with pytest.raises(ValidationError, match="finite"):
        StatisticalDesignRevision.model_validate(values)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"subject_order": ()}, "non-empty"),
        ({"subject_order": ("sub-01", "sub-01", "sub-03")}, "unique"),
        ({"contrast": (0.0, 0.0)}, "non-zero"),
        ({"contrast": (1.0,)}, "does not match"),
        ({"one_sample_baseline": None}, "explicit baseline"),
        (
            {"images": (*one_sample().images[:-1], one_sample().images[0])},
            "Artifact IDs",
        ),
    ],
)
def test_statistical_design_rejects_ambiguous_structure(
    updates: dict[str, object], message: str
) -> None:
    values = one_sample().model_dump()
    values.update(updates)
    with pytest.raises(ValidationError, match=message):
        StatisticalDesignRevision.model_validate(values)


def test_independent_design_supports_explicit_within_group_centering() -> None:
    subjects = ("s1", "s2", "s3", "s4")
    images = tuple(
        AnalysisImage(
            subject_id=subject,
            artifact_id=f"image-{subject}",
            group="case" if index < 2 else "control",
            condition=None,
        )
        for index, subject in enumerate(subjects)
    )
    age = CovariateColumn(
        name="age",
        values=tuple(
            CovariateValue(subject_id=subject, value=value)
            for subject, value in zip(subjects, (20.0, 30.0, 40.0, 60.0), strict=True)
        ),
        centering=Centering.WITHIN_GROUP,
    )
    design = StatisticalDesignRevision(
        revision_id="independent-1",
        test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
        subject_order=subjects,
        images=images,
        group_order=("case", "control"),
        condition_order=(),
        covariates=(age,),
        contrast=(1.0, 0.0, 0.0),
        one_sample_baseline=None,
        mask_artifact_id="mask",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id="qc",
        qc_review_hash="q" * 64,
    )
    from neuroagent.domain.fmri import design_matrix

    assert design_matrix(design) == (
        (1.0, 1.0, -5.0),
        (1.0, 1.0, 5.0),
        (-1.0, 1.0, -10.0),
        (-1.0, 1.0, 10.0),
    )
    from neuroagent.tools import DpabiV82Adapter

    assert DpabiV82Adapter().project_statistics(design).other_covariates_by_group == (
        ((-5.0,), (5.0,)),
        ((-10.0,), (10.0,)),
    )
