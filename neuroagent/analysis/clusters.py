"""DPABI cluster table parsing and atlas-coordinate matching."""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable
from pathlib import Path

from neuroagent.analysis.models import AtlasPoint, ClusterLocalization, ClusterRecord


def _read(path: Path) -> list[dict[str, str]]:
    raw = path.read_text(encoding="utf-8-sig", errors="strict").splitlines()
    if not raw:
        return []
    delimiter = "\t" if "\t" in raw[0] else ","
    return [dict(row) for row in csv.DictReader(raw, delimiter=delimiter)]


def _value(row: dict[str, str], aliases: tuple[str, ...], required: bool = True) -> str | None:
    lowered = {key.strip().lower(): value for key, value in row.items()}
    for alias in aliases:
        if alias in lowered and lowered[alias].strip():
            return lowered[alias].strip()
    if required:
        raise ValueError(f"cluster table column missing: {aliases[0]}")
    return None


def parse_cluster_table(path: Path) -> tuple[ClusterRecord, ...]:
    records: list[ClusterRecord] = []
    for index, row in enumerate(_read(path), start=1):
        cluster_id = _value(row, ("cluster", "cluster id", "cluster_id"), required=False) or str(
            index
        )
        records.append(
            ClusterRecord(
                cluster_id=cluster_id,
                peak_x=float(_value(row, ("peak x", "x", "x(mm)")) or 0),
                peak_y=float(_value(row, ("peak y", "y", "y(mm)")) or 0),
                peak_z=float(_value(row, ("peak z", "z", "z(mm)")) or 0),
                voxel_count=(
                    int(float(_value(row, ("voxels", "voxel count", "size"), False) or 0)) or None
                ),
                statistic=(
                    float(_value(row, ("t", "z", "statistic", "peak statistic"), False) or 0)
                    or None
                ),
            )
        )
    if not records:
        raise ValueError("cluster table is empty")
    return tuple(records)


def localize_clusters(
    clusters: Iterable[ClusterRecord], atlas: Iterable[AtlasPoint], *, max_distance_mm: float = 8
) -> tuple[ClusterLocalization, ...]:
    points = tuple(atlas)
    if not points:
        return tuple(ClusterLocalization(cluster=item, confidence=0) for item in clusters)
    output: list[ClusterLocalization] = []
    for cluster in clusters:
        compatible = tuple(
            point for point in points if point.coordinate_space == cluster.coordinate_space
        )
        if not compatible:
            output.append(ClusterLocalization(cluster=cluster, atlas_label=None, confidence=0))
            continue
        nearest = min(
            compatible,
            key=lambda point: math.dist(
                (cluster.peak_x, cluster.peak_y, cluster.peak_z),
                (point.x, point.y, point.z),
            ),
        )
        distance = math.dist(
            (cluster.peak_x, cluster.peak_y, cluster.peak_z),
            (nearest.x, nearest.y, nearest.z),
        )
        confidence = (
            max(0.0, 1.0 - distance / max_distance_mm) if distance <= max_distance_mm else 0.0
        )
        output.append(
            ClusterLocalization(
                cluster=cluster,
                atlas_label=nearest.label if confidence else None,
                distance_mm=distance,
                confidence=confidence,
            )
        )
    return tuple(output)
