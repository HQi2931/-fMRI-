"""Assemble a frozen StatisticalDesignRevision from explicit choices and data.

The domain model already enforces the scientific rules (alignment, pairing,
group/condition order, canonical contrast, full-rank design matrix, residual
degrees of freedom).  This module does not re-implement any of them.  Its job is
the part the domain cannot do: decide **where each value comes from**.

The split is deliberate and is what makes a conversational statistics flow safe:

* The model (or the user) chooses the *method* and names the *columns*:
  which test, which grouping column, which covariate columns and their centering,
  the tail, and the correction parameters.
* The **server** supplies every data-bound value: the subject order, the
  registered metric artifact per subject, the mask artifact, and the covariate
  values read from the registered demographics revision.

A model therefore never invents a subgroup membership or a covariate number; it
can only reference a column the server already holds.  Anything the domain
rejects is returned as a blocking issue rather than raised, so the caller can
show the user exactly what has to change.
"""

from __future__ import annotations

# ruff: noqa: RUF001
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from neuroagent.domain.fmri.statistics import (
    AnalysisImage,
    Centering,
    CorrectionSpec,
    CovariateColumn,
    CovariateValue,
    FdrCorrection,
    GrfCorrection,
    GrfSmoothnessMode,
    MissingValuePolicy,
    StatisticalDesignRevision,
    StatisticalMapType,
    StatisticalQcSource,
    StatisticalTest,
    Tail,
    residual_degrees_of_freedom,
)

_ONE_SAMPLE_CANONICAL_CONTRAST = (1.0,)
_TWO_GROUP_CANONICAL_CONTRAST = (1.0, 0.0)


class CovariateChoice(BaseModel):
    """A covariate the caller asks for, by column name only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    centering: Centering = Centering.GRAND_MEAN


class StatisticsChoices(BaseModel):
    """The method-level decisions a model or user may legitimately make."""

    model_config = ConfigDict(extra="forbid")

    test: StatisticalTest
    grouping_column: str | None = Field(default=None, max_length=100)
    group_order: tuple[str, ...] = ()
    target_predictor: str | None = Field(default=None, max_length=100)
    condition_column: str | None = Field(default=None, max_length=100)
    condition_order: tuple[str, ...] = ()
    covariates: tuple[CovariateChoice, ...] = ()
    tail: Tail = Tail.TWO_SIDED
    one_sample_baseline: float | None = None
    missing_value_policy: MissingValuePolicy = MissingValuePolicy.ERROR


class DesignSubject(BaseModel):
    """One server-sourced subject row, already ordered by the QC review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    group: str | None = None
    condition: str | None = None


class DesignIssue(BaseModel):
    """A blocking reason the design could not be assembled."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class QcBinding(BaseModel):
    """Which QC decision this design is bound to; never inferred."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: StatisticalQcSource
    qc_review_revision_id: str | None = None
    qc_review_hash: str | None = None
    qc_skip_id: str | None = None
    qc_skip_hash: str | None = None
    qc_source_run_id: str | None = None
    external_import_id: str | None = None
    external_import_hash: str | None = None


class ArtifactRef(BaseModel):
    """The only artifact facts the resolver needs, decoupled from the API view."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    subject_id: str | None = None


class DesignSources(BaseModel):
    """Server-sourced rows for the assembler, already in the frozen order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subjects: tuple[DesignSubject, ...]
    mask_artifact_id: str = Field(min_length=1)
    subject_order: tuple[str, ...]


def collect_design_sources(
    *,
    artifacts: Sequence[ArtifactRef],
    included_subject_ids: Sequence[str],
    metric_artifact_type: str,
    mask_artifact_type: str,
    label_column: str | None = None,
    label_values: Mapping[str, str | None] | None = None,
) -> tuple[DesignSources | None, tuple[DesignIssue, ...]]:
    """Resolve per-subject images and the mask from a run's registered artifacts.

    The QC decision supplies the subject order and the inclusion set; the
    artifacts only supply identity.  Nothing here sorts by path or by artifact
    creation time, because file-system order must never decide who is in which
    group or which image pairs with which subject.
    """

    if not included_subject_ids:
        return None, (
            _issue(
                "no_included_subjects",
                "该运行没有已纳入的受试者；统计设计需要已批准 QC 或已记录 QC 跳过的受试者清单。",
            ),
        )

    by_subject: dict[str, list[ArtifactRef]] = {}
    for artifact in artifacts:
        if artifact.artifact_type != metric_artifact_type or artifact.subject_id is None:
            continue
        by_subject.setdefault(artifact.subject_id, []).append(artifact)

    masks = [item for item in artifacts if item.artifact_type == mask_artifact_type]
    if not masks:
        return None, (
            _issue("mask_artifact_missing", f"该运行没有登记 {mask_artifact_type!r} 掩膜产物。"),
        )
    if len(masks) > 1:
        return None, (
            _issue(
                "ambiguous_mask_artifact",
                f"该运行登记了 {len(masks)} 个 {mask_artifact_type!r} 掩膜产物，"
                "无法确定使用哪一个。",
            ),
        )

    issues: list[DesignIssue] = []
    subjects: list[DesignSubject] = []
    seen: set[str] = set()
    for subject_id in included_subject_ids:
        if subject_id in seen:
            issues.append(
                _issue("duplicate_included_subject", f"受试者 {subject_id!r} 在纳入清单中重复。")
            )
            continue
        seen.add(subject_id)
        candidates = by_subject.get(subject_id, [])
        if not candidates:
            issues.append(
                _issue(
                    "missing_metric_artifact",
                    f"受试者 {subject_id!r} 没有 {metric_artifact_type!r} 产物，无法进入统计设计。",
                )
            )
            continue
        if len(candidates) > 1:
            issues.append(
                _issue(
                    "ambiguous_metric_artifact",
                    f"受试者 {subject_id!r} 有 {len(candidates)} 个 {metric_artifact_type!r} 产物，"
                    "无法确定使用哪一个。",
                )
            )
            continue
        label: str | None = None
        if label_column is not None:
            label = (label_values or {}).get(subject_id)
            if label is None:
                issues.append(
                    _issue(
                        "label_value_missing",
                        f"受试者 {subject_id!r} 在 {label_column!r} 列上没有取值，分组无法确定。",
                    )
                )
                continue
        subjects.append(
            DesignSubject(
                subject_id=subject_id,
                artifact_id=candidates[0].artifact_id,
                group=label,
            )
        )

    if issues:
        return None, tuple(issues)
    return (
        DesignSources(
            subjects=tuple(subjects),
            mask_artifact_id=masks[0].artifact_id,
            subject_order=tuple(subject.subject_id for subject in subjects),
        ),
        (),
    )


def _issue(code: str, message: str) -> DesignIssue:
    return DesignIssue(code=code, message=message)


#: Fields only the server may set.  A model-supplied patch naming any of them is
#: stripped, so a model cannot bind its own design to a run, a QC decision or a
#: manifest, and cannot pre-seal a confirmation hash.
_SERVER_OWNED_FIELDS = frozenset(
    {
        "source_run_id",
        "external_import_id",
        "external_import_hash",
        "qc_review_revision_id",
        "qc_review_hash",
        "qc_skip_id",
        "qc_skip_hash",
        "input_manifest_hash",
        "demographics_revision_id",
        "demographics_hash",
        "confirmed_hash",
    }
)


class StatisticsDraft(BaseModel):
    """Partial statistical-design choices, deliberately without server bindings."""

    model_config = ConfigDict(extra="forbid")

    test: StatisticalTest | None = None
    grouping_column: str | None = Field(default=None, max_length=100)
    group_order: list[str] = Field(default_factory=list)
    target_predictor: str | None = Field(default=None, max_length=100)
    condition_column: str | None = Field(default=None, max_length=100)
    condition_order: list[str] = Field(default_factory=list)
    covariates: list[CovariateChoice] = Field(default_factory=list)
    tail: Tail | None = None
    one_sample_baseline: float | None = None
    missing_value_policy: MissingValuePolicy | None = None
    metric_artifact_type: str | None = Field(default=None, max_length=100)
    mask_artifact_type: str | None = Field(default=None, max_length=100)
    correction_method: str | None = Field(default=None, max_length=20)
    correction_parameters: dict[str, float] = Field(default_factory=dict)
    # Server-owned bindings; never supplied by a model.
    source_run_id: str | None = None
    external_import_id: str | None = None
    external_import_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    qc_review_revision_id: str | None = None
    qc_review_hash: str | None = None
    qc_skip_id: str | None = None
    qc_skip_hash: str | None = None
    input_manifest_hash: str | None = None
    demographics_revision_id: str | None = None
    demographics_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    confirmed_hash: str | None = None

    @model_validator(mode="after")
    def correction_parameters_require_a_method(self) -> StatisticsDraft:
        if self.correction_parameters and not self.correction_method:
            raise ValueError("校正参数必须与校正方法一起提供")
        return self


def merge_statistics_draft(draft: StatisticsDraft, patch: Mapping[str, Any]) -> StatisticsDraft:
    """Apply one turn's incremental patch, ignoring server-owned fields.

    Mirrors the preprocessing draft: the model accumulates choices across turns,
    and a field it never mentions keeps whatever the user already decided.
    """

    applied = {
        key: value
        for key, value in patch.items()
        if key not in _SERVER_OWNED_FIELDS and key in StatisticsDraft.model_fields
    }
    return StatisticsDraft.model_validate({**draft.model_dump(mode="json"), **applied})


def statistics_draft_hash(draft: StatisticsDraft) -> str:
    payload = draft.model_dump(mode="json", exclude={"confirmed_hash"})
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def statistics_draft_issues(draft: StatisticsDraft) -> tuple[str, ...]:
    """What the user still has to decide before a design can be assembled."""

    issues: list[str] = []
    if draft.test is None:
        issues.append("检验类型尚未选择")
    elif draft.test is StatisticalTest.INDEPENDENT_TWO_SAMPLE_T:
        if not draft.grouping_column:
            issues.append("分组列尚未指定")
        if len(draft.group_order) != 2 or len(set(draft.group_order)) != 2:
            issues.append("分组顺序尚未明确给出两个分组")
    elif draft.test is StatisticalTest.ONE_WAY_ANOVA_ANCOVA:
        if not draft.grouping_column:
            issues.append("分组列尚未指定")
        if len(draft.group_order) < 2 or len(set(draft.group_order)) != len(draft.group_order):
            issues.append("ANOVA/ANCOVA 分组顺序尚未明确给出至少两个分组")
    elif draft.test is StatisticalTest.PAIRED_T:
        if not draft.condition_column:
            issues.append("条件列尚未指定")
        if len(draft.condition_order) != 2 or len(set(draft.condition_order)) != 2:
            issues.append("条件顺序尚未明确给出两个条件")
    elif draft.test is StatisticalTest.ONE_SAMPLE_T:
        if draft.one_sample_baseline is None:
            issues.append("单样本检验基线尚未给出")
    elif draft.test in {StatisticalTest.CORRELATION, StatisticalTest.REGRESSION}:
        if not draft.covariates:
            issues.append("相关／回归至少需要一个已登记数值预测变量")
        if not draft.target_predictor:
            issues.append("目标预测变量尚未指定")
        elif draft.target_predictor not in {item.name for item in draft.covariates}:
            issues.append("目标预测变量必须出现在已登记预测变量清单中")
    if not draft.metric_artifact_type:
        issues.append("指标产物类型尚未选择")
    if draft.correction_method and not draft.correction_parameters:
        issues.append("已选择校正方法但尚未给出校正参数")
    return tuple(issues)


def render_statistics_draft_review(draft: StatisticsDraft) -> str:
    """Render the concrete, reviewable decisions for the user to confirm."""

    lines = [
        f"- 检验类型：{draft.test.value if draft.test else '未选择'}",
        f"- 指标产物类型：{draft.metric_artifact_type or '未选择'}",
        f"- 尾部：{draft.tail.value if draft.tail else '未选择'}",
    ]
    if draft.grouping_column:
        order = "、".join(draft.group_order) or "未指定顺序"
        lines.append(f"- 分组列：{draft.grouping_column}（顺序：{order}）")
    if draft.target_predictor:
        lines.append(f"- 目标预测变量：{draft.target_predictor}")
    if draft.condition_column:
        order = "、".join(draft.condition_order) or "未指定顺序"
        lines.append(f"- 条件列：{draft.condition_column}（顺序：{order}）")
    if draft.one_sample_baseline is not None:
        lines.append(f"- 检验基线：{draft.one_sample_baseline}")
    if draft.covariates:
        rendered = "、".join(f"{item.name}（{item.centering.value}）" for item in draft.covariates)
        lines.append(f"- 协变量：{rendered}")
    else:
        lines.append("- 协变量：无")
    if draft.correction_method:
        parameters = "、".join(
            f"{key}={value}" for key, value in sorted(draft.correction_parameters.items())
        )
        lines.append(f"- 多重比较校正：{draft.correction_method}（{parameters or '无参数'}）")
    else:
        lines.append("- 多重比较校正：未校正")
    lines.append(
        "- 受试者顺序、逐受试者产物与掩膜：由服务端从已确认的外部纳入记录冻结登记，"
        "不由模型或本草稿提供"
        if draft.external_import_id
        else "- 受试者顺序、逐受试者产物与掩膜：由服务端在确认时从已批准的 QC 决定冻结登记，"
        "不由模型或本草稿提供"
    )
    return "\n".join(lines)


class StatisticalDesignProposal(BaseModel):
    """A fully assembled, reviewable design awaiting the user's exact confirmation.

    Everything here is server-sourced or derived from the frozen design, so the
    proposal hash can bind the user's confirmation to content they were shown.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    design: StatisticalDesignRevision
    correction: CorrectionSpec | None
    project_id: str = Field(min_length=1)
    project_version: int = Field(ge=1)
    input_manifest_hash: str = Field(min_length=64, max_length=64)
    source_run_id: str | None = Field(default=None, min_length=1)
    frozen_subject_order: tuple[str, ...]
    metric_artifact_ids: tuple[str, ...]
    demographics_revision_id: str | None = None
    demographics_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    review: str = Field(min_length=1)
    proposal_hash: str = Field(min_length=64, max_length=64)


def proposal_hash(
    design: StatisticalDesignRevision,
    correction: CorrectionSpec | None,
    *,
    demographics_revision_id: str | None = None,
    demographics_hash: str | None = None,
) -> str:
    """Hash exactly what the user reviews, so a stale confirmation is refused."""

    payload = {
        "design": design.model_dump(mode="json"),
        "correction": correction.model_dump(mode="json") if correction is not None else None,
        "demographics_revision_id": demographics_revision_id,
        "demographics_hash": demographics_hash,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def build_correction(
    draft: StatisticsDraft, design: StatisticalDesignRevision
) -> tuple[CorrectionSpec | None, tuple[DesignIssue, ...]]:
    """Derive the correction spec, computing degrees of freedom from the design.

    The model chooses the method and its thresholds; the degrees of freedom come
    from the frozen design, and the domain decides whether the combination is
    legal at all.
    """

    if not draft.correction_method:
        return None, ()

    parameters = draft.correction_parameters
    tail_two_sided = design.tail is Tail.TWO_SIDED
    statistic_type = (
        StatisticalMapType.F
        if design.test is StatisticalTest.ONE_WAY_ANOVA_ANCOVA
        else StatisticalMapType.R
        if design.test is StatisticalTest.CORRELATION
        else StatisticalMapType.T
    )
    numerator_df = (
        float(len(design.group_order) - 1)
        if design.test is StatisticalTest.ONE_WAY_ANOVA_ANCOVA
        else float(residual_degrees_of_freedom(design))
    )
    denominator_df = (
        float(residual_degrees_of_freedom(design))
        if design.test is StatisticalTest.ONE_WAY_ANOVA_ANCOVA
        else None
    )
    try:
        if draft.correction_method == "fdr":
            threshold = parameters.get("q_threshold")
            if threshold is None:
                return None, (
                    _issue("correction_parameter_missing", "FDR 需要 q_threshold 参数。"),
                )
            return (
                FdrCorrection(
                    method="fdr",
                    q_threshold=threshold,
                    mask_artifact_id=design.mask_artifact_id,
                    statistic_type=statistic_type,
                    df1=numerator_df,
                    df2=denominator_df,
                ),
                (),
            )
        if draft.correction_method == "grf":
            voxel = parameters.get("voxel_p_threshold")
            cluster = parameters.get("cluster_p_threshold")
            if voxel is None or cluster is None:
                return None, (
                    _issue(
                        "correction_parameter_missing",
                        "GRF 需要 voxel_p_threshold 与 cluster_p_threshold 参数。",
                    ),
                )
            return (
                GrfCorrection(
                    method="grf",
                    voxel_p_threshold=voxel,
                    cluster_p_threshold=cluster,
                    two_tailed=(
                        False
                        if design.test is StatisticalTest.ONE_WAY_ANOVA_ANCOVA
                        else tail_two_sided
                    ),
                    mask_artifact_id=design.mask_artifact_id,
                    statistic_type=statistic_type,
                    df1=numerator_df,
                    df2=denominator_df,
                    smoothness_mode=GrfSmoothnessMode.DPABI_HEADER_OR_ESTIMATE,
                    smoothness_dlh=None,
                ),
                (),
            )
    except ValidationError as exc:
        return None, tuple(
            _issue("correction_rejected_by_domain", error["msg"]) for error in exc.errors()
        )
    return None, (
        _issue(
            "correction_not_supported",
            f"校正方法 {draft.correction_method!r} 尚未由本流程支持。",
        ),
    )


def _covariate_columns(
    choices: StatisticsChoices,
    subjects: Sequence[DesignSubject],
    covariate_values: Mapping[str, Mapping[str, float | None]],
) -> tuple[tuple[CovariateColumn, ...], tuple[DesignIssue, ...]]:
    """Align each requested covariate to ``subjects`` in the frozen order."""

    columns: list[CovariateColumn] = []
    issues: list[DesignIssue] = []
    order = tuple(subject.subject_id for subject in subjects)
    covariate_choices = list(choices.covariates)
    if choices.test is StatisticalTest.CORRELATION and choices.target_predictor is not None:
        covariate_choices.sort(key=lambda item: item.name != choices.target_predictor)
    for choice in covariate_choices:
        column = covariate_values.get(choice.name)
        if column is None:
            issues.append(
                _issue(
                    "covariate_column_missing",
                    f"协变量列 {choice.name!r} 不在已登记的人口学信息中。",
                )
            )
            continue
        missing = [subject_id for subject_id in order if column.get(subject_id) is None]
        if missing and choices.missing_value_policy is MissingValuePolicy.ERROR:
            issues.append(
                _issue(
                    "covariate_value_missing",
                    f"协变量列 {choice.name!r} 在 {len(missing)} 个受试者上缺少取值；"
                    "如需排除请显式选择排除策略。",
                )
            )
            continue
        if missing:
            # Exclusion is an explicit, recorded decision: the caller must have
            # already removed those subjects from the frozen order.
            issues.append(
                _issue(
                    "covariate_value_missing",
                    f"协变量列 {choice.name!r} 仍有 {len(missing)} 个受试者缺少取值；"
                    "需要在冻结顺序之前完成显式排除。",
                )
            )
            continue
        resolved: list[CovariateValue] = []
        for subject_id in order:
            value = column.get(subject_id)
            if value is None:  # already reported above; narrows the type here
                continue
            resolved.append(CovariateValue(subject_id=subject_id, value=float(value)))
        columns.append(
            CovariateColumn(
                name=choice.name,
                values=tuple(resolved),
                centering=choice.centering,
            )
        )
    return tuple(columns), tuple(issues)


def _subject_order(subjects: Sequence[DesignSubject]) -> tuple[str, ...]:
    """Unique subject IDs in first-seen order.

    A paired design supplies one row per (subject, condition), so the frozen
    subject order is the distinct subjects, not the row count.
    """

    order: list[str] = []
    for subject in subjects:
        if subject.subject_id not in order:
            order.append(subject.subject_id)
    return tuple(order)


def _duplicate_row(subjects: Sequence[DesignSubject]) -> tuple[str, str | None] | None:
    """Find a repeated (subject, condition) row.

    Repeating a subject across conditions is expected for a paired design, so the
    duplicate key includes the condition.
    """

    seen: set[tuple[str, str | None]] = set()
    for subject in subjects:
        key = (subject.subject_id, subject.condition)
        if key in seen:
            return key
        seen.add(key)
    return None


def _images(
    choices: StatisticsChoices,
    subjects: Sequence[DesignSubject],
    subject_order: tuple[str, ...],
) -> tuple[AnalysisImage, ...]:
    """Order images the way the domain requires, without re-sorting by filename."""

    if choices.test is StatisticalTest.PAIRED_T:
        by_condition: dict[str | None, dict[str, DesignSubject]] = {}
        for subject in subjects:
            by_condition.setdefault(subject.condition, {})[subject.subject_id] = subject
        return tuple(
            AnalysisImage(
                subject_id=subject_id,
                artifact_id=by_condition[condition][subject_id].artifact_id,
                group=None,
                condition=condition,
            )
            for condition in choices.condition_order
            for subject_id in subject_order
        )
    return tuple(
        AnalysisImage(
            subject_id=subject.subject_id,
            artifact_id=subject.artifact_id,
            group=subject.group,
            condition=None,
        )
        for subject in subjects
    )


def _declared_groups(subjects: Sequence[DesignSubject]) -> tuple[str, ...]:
    return tuple(sorted({subject.group for subject in subjects if subject.group is not None}))


def _column_count(choices: StatisticsChoices, subject_count: int) -> int:
    """Mirror ``design_column_count`` from the frozen layout, before construction."""

    covariates = len(choices.covariates)
    if choices.test is StatisticalTest.PAIRED_T:
        return 1 + subject_count + covariates
    if choices.test is StatisticalTest.INDEPENDENT_TWO_SAMPLE_T:
        return 2 + covariates
    if choices.test is StatisticalTest.ONE_WAY_ANOVA_ANCOVA:
        return len(choices.group_order) + covariates
    return 1 + covariates


def _contrast(choices: StatisticsChoices, subject_count: int) -> tuple[float, ...]:
    """The canonical first-column contrast, sized to the design's real columns.

    The DPABI t-test helpers require this exact shape, and its length must match
    the design including every covariate column.
    """

    columns = _column_count(choices, subject_count)
    if choices.test is StatisticalTest.ONE_WAY_ANOVA_ANCOVA:
        return (
            *(1.0 for _ in range(len(choices.group_order) - 1)),
            *(0.0 for _ in range(columns - len(choices.group_order) + 1)),
        )
    if choices.test is StatisticalTest.REGRESSION:
        if choices.target_predictor is None:
            return tuple(0.0 for _ in range(columns))
        index = next(
            (
                position + 1
                for position, item in enumerate(choices.covariates)
                if item.name == choices.target_predictor
            ),
            -1,
        )
        return tuple(1.0 if column == index else 0.0 for column in range(columns))
    return (1.0, *(0.0 for _ in range(columns - 1)))


def assemble_statistical_design(
    *,
    choices: StatisticsChoices,
    subjects: Sequence[DesignSubject],
    covariate_values: Mapping[str, Mapping[str, float | None]],
    mask_artifact_id: str,
    revision_id: str,
    qc: QcBinding,
) -> tuple[StatisticalDesignRevision | None, tuple[DesignIssue, ...]]:
    """Return the frozen design, or the blocking issues that prevent it.

    Never raises for bad input: a conversational flow has to explain the problem
    to the user, and the domain already decides what counts as invalid.
    """

    if not subjects:
        return None, (
            _issue(
                "no_subjects",
                "没有可纳入的受试者；统计设计必须在已批准 QC 或已记录 QC 跳过的运行上建立。",
            ),
        )

    duplicate = _duplicate_row(subjects)
    if duplicate is not None:
        return None, (
            _issue(
                "duplicate_subject",
                f"受试者 {duplicate[0]!r} 在条件 {duplicate[1]!r} 上出现多次，"
                "无法建立显式对齐的统计设计。",
            ),
        )

    subject_order = _subject_order(subjects)
    issues: list[DesignIssue] = []
    if choices.test in {
        StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
        StatisticalTest.ONE_WAY_ANOVA_ANCOVA,
    }:
        if choices.grouping_column is None:
            issues.append(_issue("grouping_column_required", "组间统计必须指定分组列。"))
        declared = choices.group_order or _declared_groups(subjects)
        valid_group_count = (
            len(declared) == 2
            if choices.test is StatisticalTest.INDEPENDENT_TWO_SAMPLE_T
            else len(declared) >= 2
        )
        if not valid_group_count:
            issues.append(
                _issue(
                    "group_order_unsupported",
                    "两独立样本 t 检验需要恰好两个分组；ANOVA/ANCOVA 至少需要两个。"
                    "分组顺序必须显式给出。",
                )
            )
        unlabelled = [subject.subject_id for subject in subjects if subject.group is None]
        if unlabelled:
            issues.append(
                _issue(
                    "group_label_missing",
                    f"{len(unlabelled)} 个受试者在分组列上没有取值。",
                )
            )
    elif choices.test is StatisticalTest.PAIRED_T:
        if choices.condition_column is None:
            issues.append(_issue("condition_column_required", "配对 t 检验必须指定条件列。"))
        if len(choices.condition_order) != 2 or len(set(choices.condition_order)) != 2:
            issues.append(
                _issue(
                    "condition_order_unsupported",
                    "配对 t 检验需要两个条件，且条件顺序必须显式给出。",
                )
            )
        else:
            covered = {(subject.subject_id, subject.condition) for subject in subjects}
            incomplete = [
                subject_id
                for subject_id in subject_order
                if any(
                    (subject_id, condition) not in covered for condition in choices.condition_order
                )
            ]
            if incomplete:
                issues.append(
                    _issue(
                        "condition_coverage_incomplete",
                        f"{len(incomplete)} 个受试者并非在每个条件上都有影像，配对设计被阻断。",
                    )
                )
    elif choices.test is StatisticalTest.ONE_SAMPLE_T:
        if choices.one_sample_baseline is None:
            issues.append(_issue("baseline_required", "单样本 t 检验必须显式给出检验基线。"))
    elif choices.test in {StatisticalTest.CORRELATION, StatisticalTest.REGRESSION}:
        if not choices.covariates:
            issues.append(
                _issue("predictor_required", "相关／回归至少需要一个已登记数值预测变量。")
            )
        elif choices.target_predictor not in {item.name for item in choices.covariates}:
            issues.append(
                _issue(
                    "target_predictor_required",
                    "目标预测变量必须是本方案明确列出的数值预测变量。",
                )
            )
        if choices.test is StatisticalTest.REGRESSION and choices.target_predictor is None:
            issues.append(_issue("target_predictor_required", "多变量回归需要指定一个目标系数。"))
    else:
        issues.append(
            _issue(
                "test_not_assembled",
                f"{choices.test.value} 尚未由本装配器支持，请使用专属流程。",
            )
        )
    if issues:
        return None, tuple(issues)

    columns, covariate_issues = _covariate_columns(choices, subjects, covariate_values)
    if covariate_issues:
        return None, covariate_issues

    group_order = (
        choices.group_order
        if choices.test
        in {
            StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
            StatisticalTest.ONE_WAY_ANOVA_ANCOVA,
        }
        and choices.group_order
        else (
            _declared_groups(subjects)
            if choices.test
            in {
                StatisticalTest.INDEPENDENT_TWO_SAMPLE_T,
                StatisticalTest.ONE_WAY_ANOVA_ANCOVA,
            }
            else ()
        )
    )
    condition_order = choices.condition_order if choices.test is StatisticalTest.PAIRED_T else ()

    try:
        design = StatisticalDesignRevision(
            revision_id=revision_id,
            test=choices.test,
            subject_order=subject_order,
            images=_images(choices, subjects, subject_order),
            group_order=group_order,
            condition_order=condition_order,
            covariates=columns,
            contrast=_contrast(choices, len(subject_order)),
            one_sample_baseline=(
                choices.one_sample_baseline
                if choices.test is StatisticalTest.ONE_SAMPLE_T
                else None
            ),
            mask_artifact_id=mask_artifact_id,
            tail=choices.tail,
            missing_value_policy=choices.missing_value_policy,
            qc_source=qc.source,
            qc_review_revision_id=qc.qc_review_revision_id,
            qc_review_hash=qc.qc_review_hash,
            qc_skip_id=qc.qc_skip_id,
            qc_skip_hash=qc.qc_skip_hash,
            qc_source_run_id=qc.qc_source_run_id,
            external_import_id=qc.external_import_id,
            external_import_hash=qc.external_import_hash,
        )
    except ValidationError as exc:
        return None, tuple(
            _issue("design_rejected_by_domain", error["msg"]) for error in exc.errors()
        )
    return design, ()
