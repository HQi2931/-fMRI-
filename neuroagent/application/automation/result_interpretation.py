"""Durable, proposal-only interpretation of verified statistical results."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from enum import StrEnum
from itertools import islice
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ResultInterpretationStatus(StrEnum):
    READY = "ready"
    COMPLETED = "completed"
    WAITING_USER = "waiting_user"
    BLOCKED = "blocked"


class StatisticalResultInterpretationOutbox(BaseModel):
    """Immutable result/task binding plus a retryable delivery state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=100)
    conversation_id: str = Field(min_length=1, max_length=100)
    project_id: str = Field(min_length=1, max_length=100)
    run_id: str = Field(min_length=1, max_length=100)
    plan_revision_id: str = Field(min_length=1, max_length=100)
    result_id: str = Field(min_length=1, max_length=100)
    result_bundle_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_version: int = Field(ge=1)
    artifact_ids: tuple[str, ...] = Field(min_length=1, max_length=20_000)
    status: ResultInterpretationStatus = ResultInterpretationStatus.READY
    lease_owner: str | None = Field(default=None, max_length=200)
    lease_expires_at: float | None = None
    fencing_token: int = Field(default=0, ge=0)
    attempt_count: int = Field(default=0, ge=0)
    retry_generation: int = Field(default=0, ge=0)
    next_attempt_at: float | None = None
    last_error_code: str | None = Field(default=None, max_length=64)
    continuation_message_id: str | None = Field(default=None, max_length=100)


class ResultInterpretationClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    worker_id: str
    lease_expires_at: float
    fencing_token: int


class ResultInterpretationRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=100)


class VerifiedResultInterpretationSource(BaseModel):
    """Exact identity and bounded aggregate facts; excludes paths and subject rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    conversation_id: str
    project_id: str
    run_id: str
    plan_revision_id: str
    result_id: str
    result_bundle_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    result_version: int = Field(ge=1)
    artifact_ids: tuple[str, ...] = Field(min_length=1, max_length=20_000)
    source_evidence_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_facts: ResultInterpretationModelFacts
    deterministic_readout: str = Field(min_length=1, max_length=8_000)


class ResultInterpretationGroupCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1, max_length=48)
    count: int = Field(ge=0, le=20_000)


class ResultInterpretationCovariate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    centering: str = Field(min_length=1, max_length=40)


class ResultInterpretationMethodFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    test_label: str = Field(min_length=1, max_length=80)
    statistic_type: str = Field(min_length=1, max_length=8)
    subject_count: int = Field(ge=0, le=20_000)
    image_count: int = Field(ge=0, le=40_000)
    group_counts: tuple[ResultInterpretationGroupCount, ...] = Field(max_length=12)
    groups_truncated: bool
    condition_order: tuple[str, ...] = Field(max_length=8)
    conditions_truncated: bool
    covariates: tuple[ResultInterpretationCovariate, ...] = Field(max_length=12)
    covariates_truncated: bool
    contrast: tuple[float, ...] = Field(max_length=16)
    contrast_truncated: bool
    tail_label: str = Field(min_length=1, max_length=40)
    positive_direction: str | None = Field(default=None, max_length=120)
    negative_direction: str | None = Field(default=None, max_length=120)
    df1: float | None = Field(default=None, allow_inf_nan=False)
    df2: float | None = Field(default=None, allow_inf_nan=False)
    correction_method: str | None = Field(default=None, max_length=24)
    q_threshold: float | None = Field(default=None, allow_inf_nan=False)
    actual_p_threshold: float | None = Field(default=None, allow_inf_nan=False)
    voxel_p_threshold: float | None = Field(default=None, allow_inf_nan=False)
    cluster_p_threshold: float | None = Field(default=None, allow_inf_nan=False)
    assumptions: tuple[str, ...] = Field(max_length=4)
    assumptions_truncated: bool
    labels_truncated: bool


class ResultInterpretationClusterFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cluster_index: int = Field(ge=1, le=20)
    extent_voxels: int = Field(ge=0, le=100_000_000)
    peak_statistic: float = Field(allow_inf_nan=False)
    peak_coordinate_mm: tuple[float, float, float]
    coordinate_space: str = Field(max_length=24)
    inference: str = Field(max_length=32)
    direction: str | None = Field(default=None, max_length=120)


class ResultInterpretationModelFacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str = Field(max_length=32)
    non_scientific: bool
    method: ResultInterpretationMethodFacts | None
    inference_status: str = Field(max_length=40)
    significant_voxel_count: int | None = Field(default=None, ge=0, le=1_000_000_000)
    cluster_count: int = Field(ge=0, le=20_000)
    clusters_truncated: bool
    clusters: tuple[ResultInterpretationClusterFacts, ...] = Field(max_length=20)
    localization_status: str = Field(max_length=40)
    interpretation_limits: tuple[str, ...] = Field(max_length=4)


def _bounded_text(value: Any, limit: int, *, fallback: str = "") -> tuple[str, bool]:
    text = value if isinstance(value, str) else fallback
    return text[:limit], len(text) > limit


def _bounded_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _take_limited(values: Any, limit: int) -> tuple[list[Any], bool]:
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
        return [], False
    selected = list(islice(iter(values), limit + 1))
    return selected[:limit], len(selected) > limit


def project_result_model_facts(facts: Mapping[str, Any]) -> ResultInterpretationModelFacts:
    """Whitelist and cap every value sent to the result-interpretation model."""
    raw_method = facts.get("method")
    method: ResultInterpretationMethodFacts | None = None
    if isinstance(raw_method, Mapping):
        raw_groups = raw_method.get("group_counts")
        group_items, groups_over_limit = (
            _take_limited(raw_groups.items(), 12)
            if isinstance(raw_groups, Mapping)
            else ([], False)
        )
        groups: list[ResultInterpretationGroupCount] = []
        groups_truncated = groups_over_limit
        for label, count in group_items:
            bounded_label, was_clipped = _bounded_text(label, 48, fallback="(未知组)")
            groups_truncated = groups_truncated or was_clipped
            if isinstance(count, bool) or not isinstance(count, int):
                continue
            groups.append(
                ResultInterpretationGroupCount(label=bounded_label or "(空组名)", count=count)
            )

        raw_conditions = raw_method.get("condition_order")
        condition_values, conditions_over_limit = _take_limited(raw_conditions, 8)
        conditions = []
        conditions_truncated = conditions_over_limit
        for value in condition_values:
            clipped, was_clipped = _bounded_text(value, 64, fallback="(未知条件)")
            conditions.append(clipped or "(空条件)")
            conditions_truncated = conditions_truncated or was_clipped

        raw_covariates = raw_method.get("covariates")
        covariate_values, covariates_over_limit = _take_limited(raw_covariates, 12)
        covariates: list[ResultInterpretationCovariate] = []
        covariates_truncated = covariates_over_limit
        for value in covariate_values:
            if not isinstance(value, Mapping):
                continue
            name, name_clipped = _bounded_text(value.get("name"), 64, fallback="(未知协变量)")
            centering, centering_clipped = _bounded_text(
                value.get("centering"), 40, fallback="(未知中心化)"
            )
            covariates.append(
                ResultInterpretationCovariate(
                    name=name or "(空协变量名)", centering=centering or "(未知中心化)"
                )
            )
            covariates_truncated = covariates_truncated or name_clipped or centering_clipped

        raw_contrast = raw_method.get("contrast")
        contrast_values, contrast_over_limit = _take_limited(raw_contrast, 16)
        contrast = tuple(
            value for raw in contrast_values[:16] if (value := _bounded_float(raw)) is not None
        )
        raw_assumptions = raw_method.get("assumptions")
        assumption_values, assumptions_over_limit = _take_limited(raw_assumptions, 4)
        assumptions: list[str] = []
        assumptions_truncated = assumptions_over_limit
        for value in assumption_values:
            clipped, was_clipped = _bounded_text(value, 200)
            if clipped:
                assumptions.append(clipped)
            assumptions_truncated = assumptions_truncated or was_clipped

        scalar_fields = {
            "subject_count": (0, 20_000),
            "image_count": (0, 40_000),
        }
        counts = {
            key: min(bounds[1], max(bounds[0], int(raw_method.get(key, 0))))
            for key, bounds in scalar_fields.items()
            if isinstance(raw_method.get(key), int) and not isinstance(raw_method.get(key), bool)
        }

        test_label, test_label_clipped = _bounded_text(
            raw_method.get("test_label"), 80, fallback="未记录检验"
        )
        statistic_type, statistic_type_clipped = _bounded_text(
            raw_method.get("statistic_type"), 8, fallback="未知"
        )
        tail_label, tail_label_clipped = _bounded_text(
            raw_method.get("tail_label"), 40, fallback="未记录尾部"
        )
        positive, positive_clipped = _bounded_text(raw_method.get("positive_direction"), 120)
        negative, negative_clipped = _bounded_text(raw_method.get("negative_direction"), 120)
        method = ResultInterpretationMethodFacts(
            test_label=test_label or "未记录检验",
            statistic_type=statistic_type or "未知",
            subject_count=counts.get("subject_count", 0),
            image_count=counts.get("image_count", 0),
            group_counts=tuple(groups),
            groups_truncated=groups_truncated,
            condition_order=tuple(conditions),
            conditions_truncated=conditions_truncated,
            covariates=tuple(covariates),
            covariates_truncated=covariates_truncated,
            contrast=contrast,
            contrast_truncated=contrast_over_limit,
            tail_label=tail_label or "未记录尾部",
            positive_direction=positive or None,
            negative_direction=negative or None,
            df1=_bounded_float(raw_method.get("df1")),
            df2=_bounded_float(raw_method.get("df2")),
            correction_method=(
                raw_method.get("correction_method")
                if raw_method.get("correction_method") in {"fdr", "grf"}
                else None
            ),
            q_threshold=_bounded_float(raw_method.get("q_threshold")),
            actual_p_threshold=_bounded_float(raw_method.get("actual_p_threshold")),
            voxel_p_threshold=_bounded_float(raw_method.get("voxel_p_threshold")),
            cluster_p_threshold=_bounded_float(raw_method.get("cluster_p_threshold")),
            assumptions=tuple(assumptions),
            assumptions_truncated=assumptions_truncated or positive_clipped or negative_clipped,
            labels_truncated=test_label_clipped or statistic_type_clipped or tail_label_clipped,
        )

    raw_clusters = facts.get("clusters")
    cluster_values, clusters_over_limit = _take_limited(raw_clusters, 20)
    clusters: list[ResultInterpretationClusterFacts] = []
    for index, raw in enumerate(cluster_values[:20], start=1):
        if not isinstance(raw, Mapping):
            continue
        coords_raw = raw.get("peak_coordinate_mm")
        if not isinstance(coords_raw, (list, tuple)) or len(coords_raw) != 3:
            continue
        coord_x, coord_y, coord_z = (_bounded_float(value) for value in coords_raw)
        peak = _bounded_float(raw.get("peak_statistic"))
        if peak is None or coord_x is None or coord_y is None or coord_z is None:
            continue
        coordinate_space, _ = _bounded_text(raw.get("coordinate_space"), 24, fallback="unknown")
        inference, _ = _bounded_text(raw.get("inference"), 32, fallback="unknown")
        direction, _ = _bounded_text(raw.get("direction"), 120)
        extent = raw.get("extent_voxels")
        if isinstance(extent, bool) or not isinstance(extent, int):
            continue
        clusters.append(
            ResultInterpretationClusterFacts(
                cluster_index=index,
                extent_voxels=min(100_000_000, max(0, extent)),
                peak_statistic=peak,
                peak_coordinate_mm=(coord_x, coord_y, coord_z),
                coordinate_space=coordinate_space,
                inference=inference,
                direction=direction or None,
            )
        )
    raw_limits = facts.get("interpretation_limits")
    limits_values, _limits_over_limit = _take_limited(raw_limits, 4)
    limits = tuple(_bounded_text(value, 200)[0] for value in limits_values)
    mode, _ = _bounded_text(facts.get("mode"), 32, fallback="unknown")
    inference_status, _ = _bounded_text(facts.get("inference_status"), 40, fallback="unknown")
    localization_status, _ = _bounded_text(facts.get("localization_status"), 40, fallback="unknown")
    significant_voxels = facts.get("significant_voxel_count")
    if isinstance(significant_voxels, bool) or not isinstance(significant_voxels, int):
        significant_voxels = None
    else:
        significant_voxels = min(1_000_000_000, max(0, significant_voxels))
    raw_cluster_count = facts.get("cluster_count")
    cluster_count = (
        min(20_000, max(0, raw_cluster_count))
        if isinstance(raw_cluster_count, int) and not isinstance(raw_cluster_count, bool)
        else len(cluster_values) + int(clusters_over_limit)
    )
    return ResultInterpretationModelFacts(
        mode=mode,
        non_scientific=bool(facts.get("non_scientific")),
        method=method,
        inference_status=inference_status,
        significant_voxel_count=significant_voxels,
        cluster_count=cluster_count,
        clusters_truncated=clusters_over_limit,
        clusters=tuple(clusters),
        localization_status=localization_status,
        interpretation_limits=limits,
    )
