from __future__ import annotations

import pytest
from pydantic import ValidationError

from neuroagent.domain.fmri import (
    AnalysisImage,
    Centering,
    CovariateColumn,
    CovariateValue,
    FdrCorrection,
    GrfCorrection,
    GrfSmoothnessMode,
    MissingValuePolicy,
    StatisticalDesignRevision,
    StatisticalMapType,
    StatisticalTest,
    Tail,
    design_matrix,
    residual_degrees_of_freedom,
    validate_correction_for_design,
)
from neuroagent.domain.fmri.qc import QcCheck, QcReviewRevision, QcSeverity, assert_statistics_ready


def covariate(name: str, values: tuple[float, ...]) -> CovariateColumn:
    return CovariateColumn(
        name=name,
        values=tuple(
            CovariateValue(subject_id=subject_id, value=value)
            for subject_id, value in zip(("sub-01", "sub-02", "sub-03"), values, strict=True)
        ),
        centering=Centering.GRAND_MEAN,
    )


def one_sample() -> StatisticalDesignRevision:
    return StatisticalDesignRevision(
        revision_id="stats-1",
        test=StatisticalTest.ONE_SAMPLE_T,
        subject_order=("sub-01", "sub-02", "sub-03"),
        images=tuple(
            AnalysisImage(
                subject_id=subject_id,
                artifact_id=f"image-{subject_id}",
                group=None,
                condition=None,
            )
            for subject_id in ("sub-01", "sub-02", "sub-03")
        ),
        group_order=(),
        condition_order=(),
        covariates=(covariate("age", (20.0, 30.0, 40.0)),),
        contrast=(1.0, 0.0),
        one_sample_baseline=0.0,
        mask_artifact_id="mask-001",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id="qc-1",
        qc_review_hash="q" * 64,
    )


def test_one_sample_matrix_uses_frozen_order() -> None:
    assert design_matrix(one_sample()) == (
        (1.0, -10.0),
        (1.0, 0.0),
        (1.0, 10.0),
    )


def test_statistical_design_rejects_constant_and_collinear_covariates() -> None:
    constant = one_sample().model_dump(mode="python")
    for item in constant["covariates"][0]["values"]:
        item["value"] = 20.0
    with pytest.raises(ValidationError, match="full column rank"):
        StatisticalDesignRevision.model_validate(constant)

    duplicated = one_sample().model_dump(mode="python")
    duplicate_column = {
        **duplicated["covariates"][0],
        "name": "age_duplicate",
    }
    duplicated["covariates"] = (*duplicated["covariates"], duplicate_column)
    duplicated["contrast"] = (1.0, 0.0, 0.0)
    with pytest.raises(ValidationError, match="collinear covariates"):
        StatisticalDesignRevision.model_validate(duplicated)


def test_correction_contract_binds_tail_map_type_and_residual_df() -> None:
    design = one_sample()
    assert residual_degrees_of_freedom(design) == 1
    fdr = FdrCorrection(
        method="fdr",
        q_threshold=0.05,
        mask_artifact_id=design.mask_artifact_id,
        statistic_type=StatisticalMapType.T,
        df1=1,
        df2=None,
    )
    validate_correction_for_design(design, fdr)

    with pytest.raises(ValueError, match="residual DF"):
        validate_correction_for_design(design, fdr.model_copy(update={"df1": 2.0}))
    one_tailed = design.model_copy(update={"tail": Tail.ONE_SIDED_NEGATIVE})
    with pytest.raises(ValueError, match="two-sided"):
        validate_correction_for_design(one_tailed, fdr)

    grf = GrfCorrection(
        method="grf",
        voxel_p_threshold=0.001,
        cluster_p_threshold=0.05,
        two_tailed=False,
        mask_artifact_id=design.mask_artifact_id,
        statistic_type=StatisticalMapType.T,
        df1=1,
        df2=None,
        smoothness_mode=GrfSmoothnessMode.DPABI_HEADER_OR_ESTIMATE,
        smoothness_dlh=None,
    )
    validate_correction_for_design(one_tailed, grf)


def test_paired_covariates_and_one_sample_unimplemented_centering_fail_closed() -> None:
    paired = _paired_design().model_dump(mode="python")
    paired["covariates"] = (
        {
            "name": "age",
            "values": (
                {"subject_id": "sub-01", "value": 20.0},
                {"subject_id": "sub-02", "value": 30.0},
            ),
            "centering": "grand_mean",
        },
    )
    paired["contrast"] = (1.0, 0.0, 0.0, 0.0)
    with pytest.raises(ValidationError, match="collinear"):
        StatisticalDesignRevision.model_validate(paired)

    sample = one_sample().model_dump(mode="python")
    sample["covariates"][0]["centering"] = "none"
    with pytest.raises(ValidationError, match="grand-mean centers"):
        StatisticalDesignRevision.model_validate(sample)


def test_image_order_must_equal_subject_order() -> None:
    data = one_sample().model_dump()
    data["images"] = list(reversed(data["images"]))
    with pytest.raises(ValidationError, match="subject_order"):
        StatisticalDesignRevision.model_validate(data)


def test_paired_images_are_ordered_by_condition_then_subject() -> None:
    subjects = ("sub-01", "sub-02")
    design = StatisticalDesignRevision(
        revision_id="paired-1",
        test=StatisticalTest.PAIRED_T,
        subject_order=subjects,
        images=tuple(
            AnalysisImage(
                subject_id=subject_id,
                artifact_id=f"{condition}-{subject_id}",
                group=None,
                condition=condition,
            )
            for condition in ("pre", "post")
            for subject_id in subjects
        ),
        group_order=(),
        condition_order=("pre", "post"),
        covariates=(),
        contrast=(1.0, 0.0, 0.0),
        one_sample_baseline=None,
        mask_artifact_id="mask-001",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id="qc-1",
        qc_review_hash="q" * 64,
    )
    assert design_matrix(design) == (
        (1.0, 1.0, 0.0),
        (1.0, 0.0, 1.0),
        (-1.0, 1.0, 0.0),
        (-1.0, 0.0, 1.0),
    )


def test_statistics_require_exact_approved_qc_order() -> None:
    review = QcReviewRevision(
        review_revision_id="qc-1",
        input_manifest_hash="a" * 64,
        metric_artifact_ids=("image-sub-01", "image-sub-02", "image-sub-03"),
        checks=(
            QcCheck(
                code="GRID_MATCH",
                severity=QcSeverity.BLOCKING,
                passed=True,
                evidence_artifact_ids=("qc-grid",),
                message="grids match",
            ),
        ),
        included_subject_ids=("sub-01", "sub-02", "sub-03"),
        excluded_subject_ids=(),
        exclusion_reasons=(),
        approved=True,
        approved_by="researcher",
        approval_reason="reviewed synthetic evidence",
        content_hash="c" * 64,
    )
    assert_statistics_ready(review, one_sample().subject_order)
    with pytest.raises(ValueError, match="frozen QC inclusion order"):
        assert_statistics_ready(review, tuple(reversed(one_sample().subject_order)))


def test_t_test_helpers_reject_noncanonical_contrast() -> None:
    values = one_sample().model_dump()
    values["contrast"] = (-1.0, 0.0)
    with pytest.raises(ValidationError, match="canonical first-column contrast"):
        StatisticalDesignRevision.model_validate(values)


def test_one_sample_rejects_group_metadata() -> None:
    values = one_sample().model_dump()
    values["group_order"] = ("case",)
    with pytest.raises(ValidationError, match="must not declare group"):
        StatisticalDesignRevision.model_validate(values)


def _paired_design() -> StatisticalDesignRevision:
    subjects = ("sub-01", "sub-02")
    return StatisticalDesignRevision(
        revision_id="paired-edges",
        test=StatisticalTest.PAIRED_T,
        subject_order=subjects,
        images=tuple(
            AnalysisImage(
                subject_id=subject_id,
                artifact_id=f"{condition}-{subject_id}",
                group=None,
                condition=condition,
            )
            for condition in ("pre", "post")
            for subject_id in subjects
        ),
        group_order=(),
        condition_order=("pre", "post"),
        covariates=(),
        contrast=(1.0, 0.0, 0.0),
        one_sample_baseline=None,
        mask_artifact_id="mask",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id="qc",
        qc_review_hash="q" * 64,
    )


def _independent_design() -> StatisticalDesignRevision:
    subjects = ("sub-01", "sub-02", "sub-03", "sub-04")
    return StatisticalDesignRevision(
        revision_id="independent-edges",
        test=StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
        subject_order=subjects,
        images=tuple(
            AnalysisImage(
                subject_id=subject_id,
                artifact_id=f"image-{subject_id}",
                group="case" if index < 2 else "control",
                condition=None,
            )
            for index, subject_id in enumerate(subjects)
        ),
        group_order=("case", "control"),
        condition_order=(),
        covariates=(),
        contrast=(1.0, 0.0),
        one_sample_baseline=None,
        mask_artifact_id="mask",
        tail=Tail.TWO_SIDED,
        missing_value_policy=MissingValuePolicy.ERROR,
        qc_review_revision_id="qc",
        qc_review_hash="q" * 64,
    )


def _regression_design() -> StatisticalDesignRevision:
    values = one_sample().model_dump(mode="python")
    values.update(
        revision_id="regression-edges",
        test=StatisticalTest.REGRESSION,
        one_sample_baseline=None,
    )
    return StatisticalDesignRevision.model_validate(values)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ("misaligned_covariate", "not aligned"),
        ("within_group", "within-group centering"),
        ("duplicate_covariate", "names must be unique"),
        ("image_label", "must not carry group/condition"),
    ],
)
def test_one_sample_rejects_misaligned_covariates_and_labels(update: str, message: str) -> None:
    values = one_sample().model_dump(mode="python")
    if update == "misaligned_covariate":
        values["covariates"][0]["values"] = tuple(reversed(values["covariates"][0]["values"]))
    elif update == "within_group":
        values["covariates"][0]["centering"] = Centering.WITHIN_GROUP
    elif update == "duplicate_covariate":
        values["covariates"] = (*values["covariates"], values["covariates"][0])
    else:
        values["images"][0]["group"] = "case"
    with pytest.raises(ValidationError, match=message):
        StatisticalDesignRevision.model_validate(values)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ("conditions", "exactly two ordered conditions"),
        ("order", "ordered by condition_order"),
        ("image_group", "must not carry group labels"),
        ("baseline", "must not declare a one-sample baseline"),
        ("group_order", "must not declare group_order"),
    ],
)
def test_paired_design_rejects_ambiguous_pairing(update: str, message: str) -> None:
    values = _paired_design().model_dump(mode="python")
    if update == "conditions":
        values["condition_order"] = ("pre",)
    elif update == "order":
        values["images"] = tuple(reversed(values["images"]))
    elif update == "image_group":
        values["images"][0]["group"] = "case"
    elif update == "baseline":
        values["one_sample_baseline"] = 0.0
    else:
        values["group_order"] = ("case", "control")
    with pytest.raises(ValidationError, match=message):
        StatisticalDesignRevision.model_validate(values)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ("baseline", "must not declare a one-sample baseline"),
        ("group_count", "requires two ordered groups"),
        ("unknown_group", "requires a declared group"),
        ("missing_group", "both declared groups"),
        ("condition_order", "must not declare condition_order"),
    ],
)
def test_independent_design_rejects_ambiguous_groups(update: str, message: str) -> None:
    values = _independent_design().model_dump(mode="python")
    if update == "baseline":
        values["one_sample_baseline"] = 0.0
    elif update == "group_count":
        values["group_order"] = ("case",)
    elif update == "unknown_group":
        values["images"][0]["group"] = "other"
    elif update == "missing_group":
        for image in values["images"]:
            image["group"] = "case"
    else:
        values["condition_order"] = ("rest",)
    with pytest.raises(ValidationError, match=message):
        StatisticalDesignRevision.model_validate(values)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ("baseline", "must not declare a one-sample baseline"),
        ("group_order", "must not declare group or condition"),
        ("image_group", "group/condition labels are not valid"),
        ("predictor", "requires at least one explicit predictor"),
    ],
)
def test_regression_rejects_nonpredictor_metadata(update: str, message: str) -> None:
    values = _regression_design().model_dump(mode="python")
    if update == "baseline":
        values["one_sample_baseline"] = 0.0
    elif update == "group_order":
        values["group_order"] = ("case",)
    elif update == "image_group":
        values["images"][0]["group"] = "case"
    else:
        values["covariates"] = ()
        values["contrast"] = (1.0,)
    with pytest.raises(ValidationError, match=message):
        StatisticalDesignRevision.model_validate(values)
