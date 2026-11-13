from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from neuroagent.analysis.cluster_roi import extract_cluster_means
from neuroagent.domain.fmri.results import ClusterRecord


def test_roi_means_use_only_verified_signed_component_and_matching_grid(tmp_path: Path) -> None:
    values = np.zeros((4, 4, 4), dtype=np.float32)
    values[1, 1, 1] = 4
    values[2, 2, 2] = 3  # Diagonal voxels are connected under 26-neighbour adjacency.
    values[2, 1, 1] = -5
    map_path = tmp_path / "statistic.nii"
    nib.save(nib.Nifti1Image(values, np.eye(4)), map_path)
    paths: list[Path] = []
    for index, offset in enumerate((0, 10)):
        image_values = np.zeros_like(values)
        image_values[1, 1, 1] = 2 + offset
        image_values[2, 2, 2] = 6 + offset
        image_values[2, 1, 1] = 999
        path = tmp_path / f"metric-{index}.nii"
        nib.save(nib.Nifti1Image(image_values, np.eye(4)), path)
        paths.append(path)
    cluster = ClusterRecord(
        cluster_id="positive",
        source_map_artifact_id="map",
        extent_voxels=2,
        peak_statistic=4,
        peak_coordinate_mm=(1, 1, 1),
        coordinate_space="MNI",
        direction=1,
    )
    assert extract_cluster_means(map_path=map_path, cluster=cluster, image_paths=tuple(paths)) == (
        4,
        14,
    )
    with pytest.raises(ValueError, match="extent"):
        extract_cluster_means(
            map_path=map_path,
            cluster=cluster.model_copy(update={"extent_voxels": 3}),
            image_paths=tuple(paths),
        )
    with pytest.raises(ValueError, match="peak"):
        extract_cluster_means(
            map_path=map_path,
            cluster=cluster.model_copy(update={"peak_statistic": 6}),
            image_paths=tuple(paths),
        )
    nib.save(nib.Nifti1Image(np.ones_like(values), np.diag([2, 2, 2, 1])), paths[0])
    with pytest.raises(ValueError, match="grids"):
        extract_cluster_means(map_path=map_path, cluster=cluster, image_paths=tuple(paths))


def test_roi_rejects_nonfinite_metric_values_inside_cluster(tmp_path: Path) -> None:
    values = np.zeros((3, 3, 3), dtype=np.float32)
    values[1, 1, 1] = -3
    map_path = tmp_path / "negative.nii"
    nib.save(nib.Nifti1Image(values, np.eye(4)), map_path)
    values[1, 1, 1] = np.nan
    metric = tmp_path / "metric.nii"
    nib.save(nib.Nifti1Image(values, np.eye(4)), metric)
    cluster = ClusterRecord(
        cluster_id="negative",
        source_map_artifact_id="map",
        extent_voxels=1,
        peak_statistic=-3,
        peak_coordinate_mm=(1, 1, 1),
        coordinate_space="MNI",
        direction=-1,
    )
    with pytest.raises(ValueError, match="invalid metric"):
        extract_cluster_means(map_path=map_path, cluster=cluster, image_paths=(metric,))


def test_roi_aligns_lossless_storage_axis_flips_and_permutations(tmp_path: Path) -> None:
    values = np.zeros((4, 5, 6), dtype=np.float32)
    values[1, 2, 3] = 5
    values[2, 3, 4] = 4
    map_path = tmp_path / "dpabi-map.nii"
    original = nib.Nifti1Image(values, np.diag([2.0, 3.0, 4.0, 1.0]))
    # Mimic DPABI radiological x storage, also permuting y/z to exercise shape.
    nib.save(original.as_reoriented(np.array([[0, -1], [2, 1], [1, 1]])), map_path)
    metric_values = np.full_like(values, 999)
    metric_values[1, 2, 3], metric_values[2, 3, 4] = 2, 8
    metric = tmp_path / "metric.nii"
    nib.save(nib.Nifti1Image(metric_values, original.affine), metric)
    cluster = ClusterRecord(
        cluster_id="axes",
        source_map_artifact_id="map",
        extent_voxels=2,
        peak_statistic=5,
        peak_coordinate_mm=(2, 6, 12),
        coordinate_space="MNI",
        direction=1,
    )
    assert extract_cluster_means(map_path=map_path, cluster=cluster, image_paths=(metric,)) == (5,)
    shifted_affine = original.affine.copy()
    shifted_affine[0, 3] = 2
    nib.save(nib.Nifti1Image(metric_values, shifted_affine), metric)
    with pytest.raises(ValueError, match="grids"):
        extract_cluster_means(map_path=map_path, cluster=cluster, image_paths=(metric,))
