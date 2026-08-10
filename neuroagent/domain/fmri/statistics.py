"""Explicit, versioned statistical designs independent of filesystem ordering."""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from neuroagent.domain.fmri.artifacts import FrozenModel


class StatisticalTest(StrEnum):
    ONE_SAMPLE_T = "one_sample_t"
    INDEPENDENT_TWO_SAMPLE_T = "independent_two_sample_t"
    PAIRED_T = "paired_t"
    CORRELATION = "correlation"
    REGRESSION = "regression"


class Tail(StrEnum):
    ONE_SIDED_POSITIVE = "one_sided_positive"
    ONE_SIDED_NEGATIVE = "one_sided_negative"
    TWO_SIDED = "two_sided"


class MissingValuePolicy(StrEnum):
    ERROR = "error"
    EXCLUDE_EXPLICITLY = "exclude_explicitly"


class Centering(StrEnum):
    NONE = "none"
    GRAND_MEAN = "grand_mean"
    WITHIN_GROUP = "within_group"


class GrfSmoothnessMode(StrEnum):
    """Explicit DPABI V8.2 smoothness source used by GRF inference."""

    DPABI_HEADER_OR_ESTIMATE = "dpabi_header_or_estimate"
    PROVIDED_DLH = "provided_dlh"


class AnalysisImage(FrozenModel):
    subject_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    group: str | None
    condition: str | None


class CovariateValue(FrozenModel):
    subject_id: str = Field(min_length=1)
    value: float

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("covariate values must be finite")
        return value


class CovariateColumn(FrozenModel):
    name: str = Field(min_length=1)
    values: tuple[CovariateValue, ...]
    centering: Centering


class StatisticalDesignRevision(FrozenModel):
    revision_id: str = Field(min_length=1)
    test: StatisticalTest
    subject_order: tuple[str, ...]
    images: tuple[AnalysisImage, ...]
    group_order: tuple[str, ...]
    condition_order: tuple[str, ...]
    covariates: tuple[CovariateColumn, ...]
    contrast: tuple[float, ...]
    one_sample_baseline: float | None
    mask_artifact_id: str = Field(min_length=1)
    tail: Tail
    missing_value_policy: MissingValuePolicy
    qc_review_revision_id: str = Field(min_length=1)
    qc_review_hash: str = Field(min_length=64, max_length=64)

    @field_validator("contrast")
    @classmethod
    def finite_contrast(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if not all(math.isfinite(item) for item in value):
            raise ValueError("contrast values must be finite")
        return value

    @model_validator(mode="after")
    def explicit_alignment(self) -> StatisticalDesignRevision:
        if not self.subject_order or len(set(self.subject_order)) != len(self.subject_order):
            raise ValueError("subject_order must be non-empty and unique")
        image_subjects = tuple(image.subject_id for image in self.images)
        artifact_ids = tuple(image.artifact_id for image in self.images)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("analysis image Artifact IDs must be unique")
        if self.test is StatisticalTest.PAIRED_T:
            self._validate_paired_images()
        elif image_subjects != self.subject_order:
            raise ValueError("images must be explicitly ordered exactly as subject_order")
        for covariate in self.covariates:
            covariate_subjects = tuple(value.subject_id for value in covariate.values)
            if covariate_subjects != self.subject_order:
                raise ValueError(f"covariate {covariate.name!r} is not aligned to subject_order")
            if covariate.centering is Centering.WITHIN_GROUP and self.test is not (
                StatisticalTest.INDEPENDENT_TWO_SAMPLE_T
            ):
                raise ValueError("within-group centering requires an independent two-sample test")
        if self.test is StatisticalTest.PAIRED_T and self.covariates:
            raise ValueError(
                "paired-test subject covariates are unsupported: repeating one value across "
                "both conditions is collinear with DPABI subject regressors"
            )
        if self.test is StatisticalTest.ONE_SAMPLE_T and any(
            column.centering is not Centering.GRAND_MEAN for column in self.covariates
        ):
            raise ValueError("DPABI V8.2 y_TTest1_Image always grand-mean centers other covariates")
        if len({column.name for column in self.covariates}) != len(self.covariates):
            raise ValueError("covariate names must be unique")
        self._validate_test_specific_fields()
        expected_columns = design_column_count(self)
        if len(self.contrast) != expected_columns:
            raise ValueError(
                f"contrast length {len(self.contrast)} does not match design columns "
                f"{expected_columns}"
            )
        if not any(value != 0 for value in self.contrast):
            raise ValueError("contrast must contain at least one non-zero value")
        if self.test in {
            StatisticalTest.ONE_SAMPLE_T,
            StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            StatisticalTest.PAIRED_T,
        }:
            canonical = (1.0, *(0.0 for _ in range(expected_columns - 1)))
            if self.contrast != canonical:
                raise ValueError(
                    "DPABI t-test helpers require the canonical first-column contrast; "
                    "reverse group_order/condition_order or select a negative tail instead"
                )
        matrix = design_matrix(self)
        if _matrix_rank(matrix) != expected_columns:
            raise ValueError(
                "statistical design matrix must have full column rank; "
                "constant or collinear covariates are not allowed"
            )
        if residual_degrees_of_freedom(self) <= 0:
            raise ValueError("statistical design must retain positive residual degrees of freedom")
        return self

    def _validate_paired_images(self) -> None:
        if len(self.condition_order) != 2 or len(set(self.condition_order)) != 2:
            raise ValueError("paired t test requires exactly two ordered conditions")
        expected = tuple(
            (subject_id, condition)
            for condition in self.condition_order
            for subject_id in self.subject_order
        )
        actual = tuple((image.subject_id, image.condition) for image in self.images)
        if actual != expected:
            raise ValueError("paired images must be ordered by condition_order, then subject_order")
        if any(image.group is not None for image in self.images):
            raise ValueError("paired t test images must not carry group labels")

    def _validate_test_specific_fields(self) -> None:
        if self.test is StatisticalTest.INDEPENDENT_TWO_SAMPLE_T:
            if self.one_sample_baseline is not None:
                raise ValueError("independent t test must not declare a one-sample baseline")
            if len(self.group_order) != 2 or len(set(self.group_order)) != 2:
                raise ValueError("independent t test requires two ordered groups")
            groups = tuple(image.group for image in self.images)
            if any(group not in self.group_order for group in groups):
                raise ValueError("every independent-test image requires a declared group")
            if not all(group in groups for group in self.group_order):
                raise ValueError("both declared groups must contain subjects")
            if self.condition_order:
                raise ValueError("independent t test must not declare condition_order")
        elif self.test is StatisticalTest.PAIRED_T:
            if self.one_sample_baseline is not None:
                raise ValueError("paired t test must not declare a one-sample baseline")
            if self.group_order:
                raise ValueError("paired t test must not declare group_order")
        elif self.test is StatisticalTest.ONE_SAMPLE_T:
            if self.one_sample_baseline is None:
                raise ValueError("one-sample t test requires an explicit baseline")
            if self.group_order or self.condition_order:
                raise ValueError("one-sample t test must not declare group or condition order")
            if any(image.group is not None or image.condition is not None for image in self.images):
                raise ValueError("one-sample t test images must not carry group/condition labels")
        else:
            if self.one_sample_baseline is not None:
                raise ValueError("correlation/regression must not declare a one-sample baseline")
            if self.group_order or self.condition_order:
                raise ValueError("this test type must not declare group or condition order")
            if any(image.group is not None or image.condition is not None for image in self.images):
                raise ValueError("group/condition labels are not valid for this test")
        if self.test in {StatisticalTest.CORRELATION, StatisticalTest.REGRESSION} and not (
            self.covariates
        ):
            raise ValueError("correlation/regression requires at least one explicit predictor")


class StatisticalMapType(StrEnum):
    T = "T"
    F = "F"
    Z = "Z"
    R = "R"


class FdrCorrection(FrozenModel):
    method: str = Field(pattern="^fdr$")
    q_threshold: float = Field(gt=0, lt=1)
    mask_artifact_id: str = Field(min_length=1)
    statistic_type: StatisticalMapType
    df1: float = Field(gt=0)
    df2: float | None

    @model_validator(mode="after")
    def df_matches_statistic(self) -> FdrCorrection:
        if self.statistic_type is StatisticalMapType.F and self.df2 is None:
            raise ValueError("F-map correction requires df2")
        return self


class GrfCorrection(FrozenModel):
    method: str = Field(pattern="^grf$")
    voxel_p_threshold: float = Field(gt=0, lt=1)
    cluster_p_threshold: float = Field(gt=0, lt=1)
    two_tailed: bool
    mask_artifact_id: str = Field(min_length=1)
    statistic_type: StatisticalMapType
    df1: float = Field(gt=0)
    df2: float | None
    smoothness_mode: GrfSmoothnessMode
    smoothness_dlh: float | None = Field(gt=0)

    @model_validator(mode="after")
    def df_matches_statistic(self) -> GrfCorrection:
        if self.statistic_type is StatisticalMapType.F and self.df2 is None:
            raise ValueError("F-map correction requires df2")
        if self.smoothness_mode is GrfSmoothnessMode.PROVIDED_DLH:
            if self.smoothness_dlh is None:
                raise ValueError("provided_dlh smoothness mode requires smoothness_dlh")
        elif self.smoothness_dlh is not None:
            raise ValueError(
                "dpabi_header_or_estimate smoothness mode must not carry smoothness_dlh"
            )
        return self


CorrectionSpec = FdrCorrection | GrfCorrection


def design_column_count(design: StatisticalDesignRevision) -> int:
    covariate_count = len(design.covariates)
    if design.test is StatisticalTest.PAIRED_T:
        return 1 + len(design.subject_order) + covariate_count
    if design.test in {
        StatisticalTest.ONE_SAMPLE_T,
        StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
    }:
        return (
            2 + covariate_count
            if design.test is StatisticalTest.INDEPENDENT_TWO_SAMPLE_T
            else 1 + covariate_count
        )
    return 1 + covariate_count


def design_matrix(design: StatisticalDesignRevision) -> tuple[tuple[float, ...], ...]:
    """Build a deterministic matrix using only the frozen subject order."""

    covariates = [_center_covariate(column, design) for column in design.covariates]
    if design.test is StatisticalTest.PAIRED_T:
        n_subjects = len(design.subject_order)
        rows: list[tuple[float, ...]] = []
        for row_index in range(n_subjects * 2):
            condition_code = 1.0 if row_index < n_subjects else -1.0
            subject_index = row_index % n_subjects
            subject_columns = tuple(
                1.0 if index == subject_index else 0.0 for index in range(n_subjects)
            )
            covariate_values = tuple(column[subject_index] for column in covariates)
            rows.append((condition_code, *subject_columns, *covariate_values))
        return tuple(rows)

    rows = []
    for index, image in enumerate(design.images):
        base: tuple[float, ...]
        if design.test is StatisticalTest.INDEPENDENT_TWO_SAMPLE_T:
            group_code = 1.0 if image.group == design.group_order[0] else -1.0
            base = (group_code, 1.0)
        elif design.test is StatisticalTest.ONE_SAMPLE_T:
            base = (1.0,)
        else:
            base = (1.0,)
        rows.append((*base, *(column[index] for column in covariates)))
    return tuple(rows)


def centered_covariate_columns(
    design: StatisticalDesignRevision,
) -> tuple[tuple[float, ...], ...]:
    """Return covariates exactly as declared by the frozen centering policy."""

    return tuple(_center_covariate(column, design) for column in design.covariates)


def residual_degrees_of_freedom(design: StatisticalDesignRevision) -> int:
    """Return the residual DF used by the DPABI V8.2 image helpers."""

    row_count = len(design.images)
    return row_count - design_column_count(design)


def _matrix_rank(matrix: tuple[tuple[float, ...], ...]) -> int:
    """Compute deterministic numerical rank after column scaling.

    Column scaling prevents a large-valued covariate from hiding an otherwise
    independent intercept.  This is a structural guard, not a replacement for
    study-specific diagnostics of near-collinearity.
    """

    if not matrix or not matrix[0]:
        return 0
    row_count = len(matrix)
    column_count = len(matrix[0])
    scales = tuple(max(abs(row[column]) for row in matrix) for column in range(column_count))
    if any(scale == 0 for scale in scales):
        return sum(scale != 0 for scale in scales)
    working = [[row[column] / scales[column] for column in range(column_count)] for row in matrix]
    tolerance = max(row_count, column_count) * 1e-12
    rank = 0
    for column in range(column_count):
        pivot = max(range(rank, row_count), key=lambda row: abs(working[row][column]))
        if abs(working[pivot][column]) <= tolerance:
            continue
        working[rank], working[pivot] = working[pivot], working[rank]
        pivot_value = working[rank][column]
        for row in range(rank + 1, row_count):
            factor = working[row][column] / pivot_value
            for remaining_column in range(column, column_count):
                working[row][remaining_column] -= factor * working[rank][remaining_column]
        rank += 1
        if rank == row_count:
            break
    return rank


def validate_correction_for_design(
    design: StatisticalDesignRevision,
    correction: CorrectionSpec | None,
) -> None:
    """Validate correction metadata against the statistical map actually produced.

    All currently registered statistical calls produce a T map.  Keeping this
    check in the domain lets API services and JobSpec construction share one
    deterministic scientific guard.
    """

    if correction is None:
        return
    if correction.mask_artifact_id != design.mask_artifact_id:
        raise ValueError("correction mask must equal the frozen statistical-design mask")
    if correction.statistic_type is not StatisticalMapType.T:
        raise ValueError("registered statistical designs currently produce T maps only")
    expected_df = float(residual_degrees_of_freedom(design))
    if not math.isclose(correction.df1, expected_df, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(
            f"correction df1 {correction.df1:g} does not match design residual DF {expected_df:g}"
        )
    if correction.df2 is not None:
        raise ValueError("T-map correction must not declare df2")
    if isinstance(correction, FdrCorrection):
        if design.tail is not Tail.TWO_SIDED:
            raise ValueError("DPABI V8.2 y_FDR_Image supports only two-sided T-map p values")
        return
    if correction.two_tailed != (design.tail is Tail.TWO_SIDED):
        raise ValueError("GRF two_tailed must match the statistical-design tail")


def _center_covariate(
    column: CovariateColumn, design: StatisticalDesignRevision
) -> tuple[float, ...]:
    values = tuple(item.value for item in column.values)
    if column.centering is Centering.NONE:
        return values
    if column.centering is Centering.GRAND_MEAN:
        mean = sum(values) / len(values)
        return tuple(value - mean for value in values)
    centered = list(values)
    for group in design.group_order:
        indices = [index for index, image in enumerate(design.images) if image.group == group]
        mean = sum(values[index] for index in indices) / len(indices)
        for index in indices:
            centered[index] = values[index] - mean
    return tuple(centered)
