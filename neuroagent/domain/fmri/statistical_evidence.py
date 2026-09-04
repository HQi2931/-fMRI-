"""Deterministic numerical helpers used while assembling statistical evidence."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass


def cohen_d_from_t(t_value: float, sample_count: int) -> float:
    """Return the fixed one-sample/paired effect-size definition."""

    _finite(t_value, "t_value")
    if sample_count < 1:
        raise ValueError("sample_count must be positive")
    return t_value / math.sqrt(sample_count)


def hedges_g_from_t(
    t_value: float,
    first_group_count: int,
    second_group_count: int,
    residual_df: int,
) -> float:
    """Return small-sample-corrected Hedges' g from an independent t statistic."""

    _finite(t_value, "t_value")
    if first_group_count < 1 or second_group_count < 1:
        raise ValueError("group sample counts must be positive")
    if residual_df < 1:
        raise ValueError("residual_df must be positive")
    cohen_d = t_value * math.sqrt(1.0 / first_group_count + 1.0 / second_group_count)
    correction = 1.0 - 3.0 / (4.0 * residual_df - 1.0)
    return correction * cohen_d


@dataclass(frozen=True, slots=True)
class ClusterObservation:
    cluster_id: str
    extent_voxels: int
    peak_value: float
    peak_coordinate_mm: tuple[float, float, float]


def extract_26_connected_clusters(
    values: Mapping[tuple[int, int, int], float],
    *,
    threshold: float,
    affine: tuple[tuple[float, ...], ...],
) -> tuple[ClusterObservation, ...]:
    """Extract finite supra-threshold clusters and transform peaks to mm.

    Coordinates are voxel indices.  The input mapping is intentionally small
    and format-neutral so MATLAB/NIfTI adapters can provide values without
    leaking a particular image library into the domain.
    """

    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("threshold must be finite and non-negative")
    _validate_affine(affine)
    active = {
        coordinate: value
        for coordinate, value in values.items()
        if _valid_coordinate(coordinate) and math.isfinite(value) and abs(value) >= threshold
    }
    remaining = set(active)
    clusters: list[ClusterObservation] = []
    while remaining:
        start = min(remaining)
        queue = deque([start])
        remaining.remove(start)
        members = [start]
        while queue:
            coordinate = queue.popleft()
            for neighbor in _neighbors_26(coordinate):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    queue.append(neighbor)
                    members.append(neighbor)
        peak = max(
            members,
            key=lambda item: (abs(active[item]), active[item], tuple(-v for v in item)),
        )
        clusters.append(
            ClusterObservation(
                cluster_id=f"cluster-{len(clusters) + 1:04d}",
                extent_voxels=len(members),
                peak_value=active[peak],
                peak_coordinate_mm=_apply_affine(affine, peak),
            )
        )
    return tuple(clusters)


def _neighbors_26(coordinate: tuple[int, int, int]) -> Iterator[tuple[int, int, int]]:
    x, y, z = coordinate
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                if dx or dy or dz:
                    yield x + dx, y + dy, z + dz


def _apply_affine(
    affine: tuple[tuple[float, ...], ...], coordinate: tuple[int, int, int]
) -> tuple[float, float, float]:
    vector = (*coordinate, 1.0)
    return tuple(
        sum(affine[row][column] * vector[column] for column in range(4)) for row in range(3)
    )  # type: ignore[return-value]


def _validate_affine(affine: tuple[tuple[float, ...], ...]) -> None:
    if len(affine) != 4 or any(len(row) != 4 for row in affine):
        raise ValueError("affine must be a 4x4 matrix")
    if any(not math.isfinite(value) for row in affine for value in row):
        raise ValueError("affine must contain finite values")


def _valid_coordinate(coordinate: tuple[int, int, int]) -> bool:
    return len(coordinate) == 3 and all(isinstance(value, int) for value in coordinate)


def _finite(value: float, label: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
