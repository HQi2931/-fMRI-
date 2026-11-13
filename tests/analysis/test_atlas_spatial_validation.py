from __future__ import annotations

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from scipy.io import savemat

from neuroagent.analysis import dpabi_atlas as atlas
from neuroagent.domain.fmri.results import ClusterRecord


@pytest.fixture
def spatial_fixture(tmp_path):
    templates = tmp_path / "Templates"
    templates.mkdir()
    labels = np.zeros((2, 2, 2), dtype=np.int16)
    labels[0, 0, 0], labels[1, 1, 1] = 1, 2
    for spec in atlas.DPABI_ATLASES:
        nib.save(nib.Nifti1Image(labels, np.diag([2, 2, 2, 1])), templates / spec.nifti_filename)
        savemat(
            templates / spec.labels_filename,
            {"Reference": np.array([["First region", 1], ["Second region", 2]], dtype=object)},
        )
    values = np.zeros((6, 6, 6), dtype=np.float32)
    values[1, 1, 1], values[2, 2, 2], values[3, 3, 3] = 4, 3, 2
    values[1, 2, 1], values[2, 3, 2] = -5, -3
    path = tmp_path / "statistic.nii"
    nib.save(nib.Nifti1Image(values, np.eye(4)), path)
    clusters = tuple(
        ClusterRecord(
            cluster_id=name,
            source_map_artifact_id="map",
            extent_voxels=extent,
            peak_statistic=peak,
            peak_coordinate_mm=coordinate,
            coordinate_space="MNI",
            direction=direction,
        )
        for name, extent, peak, coordinate, direction in (
            ("positive", 3, 4, (1, 1, 1), 1),
            ("negative", 2, -5, (1, 2, 1), -1),
        )
    )
    return tmp_path, path, clusters


@pytest.mark.parametrize("atlas_id", ["aal", "aal3", "brodmann"])
def test_different_atlas_grid_preserves_signed_diagonal_components_and_background(
    spatial_fixture, atlas_id
):
    root, path, clusters = spatial_fixture
    localized = atlas.localize_cluster_map(
        dpabi_dir=root, atlas_id=atlas_id, map_path=path, clusters=clusters
    )
    positive, negative = localized
    assert positive.atlas_peak.label == "First region"
    assert positive.atlas_peak.voxel_ijk == (0, 0, 0)
    assert positive.atlas_peak.distance_mm == pytest.approx(np.sqrt(3))
    # Independent world-to-grid calculation: 1/2 rounds to 0, 2/2 to 1,
    # and 3/2 to 2 (outside). All three diagonal voxels share one 26-neighbour cluster.
    assert {row.label_value: row.voxel_count for row in positive.coverage} == {0: 1, 1: 1, 2: 1}
    assert all(row.proportion == pytest.approx(1 / 3) for row in positive.coverage)
    assert negative.atlas_peak.label_value == 0 and negative.atlas_peak.label is None
    assert [(row.label_value, row.voxel_count, row.proportion) for row in negative.coverage] == [
        (0, 2, 1)
    ]
    assert np.count_nonzero(atlas.cluster_voxel_mask(path, clusters[0])) == 3
    assert np.count_nonzero(atlas.cluster_voxel_mask(path, clusters[1])) == 2
    atlas.verify_peak_against_statistical_map(
        map_path=path, coordinate_mm=(1, 1, 1), expected_statistic=4
    )
    assert (
        atlas.sample_atlas_peak(dpabi_dir=root, atlas_id=atlas_id, coordinate_mm=(1, 1, 1))
        == positive.atlas_peak
    )
    no_label = atlas.sample_atlas_peak(
        dpabi_dir=root, atlas_id=atlas_id, coordinate_mm=(99, 99, 99)
    )
    assert no_label.voxel_ijk is None and no_label.label is None
    # F clusters carry no group direction; geometry can still be localized.
    f_cluster = clusters[0].model_copy(update={"direction": None})
    assert (
        atlas.localize_cluster_map(
            dpabi_dir=root, atlas_id=atlas_id, map_path=path, clusters=(f_cluster,)
        )[0].coverage
        == positive.coverage
    )


@pytest.mark.parametrize(
    "change,message",
    [
        ({"direction": -1}, "方向"),
        ({"peak_coordinate_mm": (99, 99, 99)}, "网格"),
        ({"peak_statistic": 8}, "数值"),
        ({"extent_voxels": 4}, "连通域"),
    ],
)
def test_tampered_cluster_evidence_cannot_produce_brain_regions(spatial_fixture, change, message):
    root, path, clusters = spatial_fixture
    changed = clusters[0].model_copy(update=change)
    with pytest.raises(ValueError, match=message):
        atlas.localize_cluster_map(
            dpabi_dir=root, atlas_id="aal", map_path=path, clusters=(changed,)
        )
    with pytest.raises(ValueError):
        atlas.cluster_voxel_mask(path, changed)


@pytest.mark.parametrize("query", ["peak", "cluster", "resolve"])
@pytest.mark.parametrize("reason", ["unconfigured", "unknown_atlas", "missing_file"])
def test_unavailable_atlas_blocks_localization(spatial_fixture, query, reason):
    root, path, clusters = spatial_fixture
    if reason == "missing_file":
        (root / "Templates" / "aal.nii").unlink()
    dpabi = None if reason == "unconfigured" else root
    atlas_id = "not-registered" if reason == "unknown_atlas" else "aal"
    with pytest.raises(ValueError):
        if query == "peak":
            atlas.sample_atlas_peak(dpabi_dir=dpabi, atlas_id=atlas_id, coordinate_mm=(1, 1, 1))
        elif query == "cluster":
            atlas.localize_cluster_map(
                dpabi_dir=dpabi, atlas_id=atlas_id, map_path=path, clusters=clusters
            )
        else:
            atlas.get_atlas(dpabi, atlas_id)
    # Absence of clusters needs no atlas and remains a normal empty result.
    assert (
        atlas.localize_cluster_map(dpabi_dir=dpabi, atlas_id=atlas_id, map_path=path, clusters=())
        == ()
    )


@pytest.mark.parametrize("corruption", ["4d", "float_labels", "invalid_label_table"])
def test_atlas_catalog_reports_unusable_files_without_exposing_paths(spatial_fixture, corruption):
    root, _, _ = spatial_fixture
    templates = root / "Templates"
    if corruption == "invalid_label_table":
        savemat(templates / "aal_Labels.mat", {"Reference": np.array([1, 2, 3])})
    else:
        values = (
            np.ones((2, 2, 2, 2), dtype=np.int16)
            if corruption == "4d"
            else np.ones((2, 2, 2), dtype=np.float32)
        )
        nib.save(nib.Nifti1Image(values, np.eye(4)), templates / "aal.nii")
    catalog = atlas.list_dpabi_atlases(root)
    assert not catalog[0].available and str(root) not in catalog[0].reason
    assert catalog[1].available and catalog[1].label_count == 2
    assert len(catalog[1].content_hash) == 64
    assert not any(row.available for row in atlas.list_dpabi_atlases(None))


@pytest.mark.parametrize("corruption", ["4d", "nonfinite"])
def test_invalid_statistic_grid_blocks_regions_and_peak_sampling(spatial_fixture, corruption):
    root, path, clusters = spatial_fixture
    values = (
        np.ones((2, 2, 2, 2), dtype=np.float32)
        if corruption == "4d"
        else np.full((6, 6, 6), np.nan, dtype=np.float32)
    )
    nib.save(nib.Nifti1Image(values, np.eye(4)), path)
    with pytest.raises(ValueError):
        atlas.localize_cluster_map(dpabi_dir=root, atlas_id="aal", map_path=path, clusters=clusters)
    with pytest.raises(ValueError):
        atlas.verify_peak_against_statistical_map(
            map_path=path, coordinate_mm=(1, 1, 1), expected_statistic=4
        )
    with pytest.raises(ValueError):
        atlas.cluster_voxel_mask(path, clusters[0])


def test_atlas_rejects_template_symlink_and_singular_affine(spatial_fixture, monkeypatch):
    root, _, _ = spatial_fixture
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "Templates" or original(path))
    assert not any(row.available for row in atlas.list_dpabi_atlases(root))
    with pytest.raises(ValueError, match="不可逆"):
        atlas._valid_affine(np.zeros((4, 4)))
    with pytest.raises(ValueError, match="无效"):
        atlas._valid_affine(np.full((4, 4), np.nan))


def test_unknown_integer_labels_remain_numeric_and_background_remains_in_denominator(
    spatial_fixture,
):
    root, path, clusters = spatial_fixture
    labels = np.zeros((2, 2, 2), dtype=np.int16)
    labels[0, 0, 0], labels[1, 1, 1] = 99, 99
    nib.save(nib.Nifti1Image(labels, np.diag([2, 2, 2, 1])), root / "Templates" / "aal.nii")
    result = atlas.localize_cluster_map(
        dpabi_dir=root, atlas_id="aal", map_path=path, clusters=clusters[:1]
    )[0]
    assert result.atlas_peak.label_value == 99 and result.atlas_peak.label is None
    assert result.coverage[0].label == "未知图谱标签"
    assert result.coverage[0].proportion == pytest.approx(2 / 3)
    assert result.coverage[1].proportion == pytest.approx(1 / 3)
