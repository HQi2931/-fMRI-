from __future__ import annotations

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from neuroagent.analysis.external_metric_import import (
    inspect_external_mask,
    inspect_external_metric_directory,
)
from neuroagent.domain.fmri.artifacts import MetricScaling
from neuroagent.domain.fmri.metrics import MetricKind


def _write_map(path: Path, *, offset: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.arange(4 * 5 * 6, dtype=np.float32).reshape((4, 5, 6)) + offset
    nib.save(nib.Nifti1Image(data, np.eye(4)), str(path))


def test_explicit_paired_layout_requires_two_complete_unique_conditions(tmp_path: Path) -> None:
    root = tmp_path / "paired"
    for condition in ("before", "after"):
        for subject in ("sub-01", "sub-02", "sub-03"):
            _write_map(root / condition / f"zALFFMap_{subject}.nii")
    grouped = inspect_external_metric_directory(root)
    assert any("重复" in issue for issue in grouped.blocking_issues)
    paired = inspect_external_metric_directory(root, layout="paired_conditions")
    assert not paired.blocking_issues
    assert paired.layout == "paired_conditions"
    assert len(paired.images) == 6
    assert {image.condition for image in paired.images} == {"before", "after"}
    (root / "after" / "zALFFMap_sub-02.nii").unlink()
    missing = inspect_external_metric_directory(root, layout="paired_conditions")
    assert any("缺失配对" in issue for issue in missing.blocking_issues)
    _write_map(root / "before" / "sub-01-zALFFMap.nii")
    duplicate = inspect_external_metric_directory(root, layout="paired_conditions")
    assert any("重复" in issue for issue in duplicate.blocking_issues)


def test_external_metric_inspection_aligns_groups_and_records_unknown_processing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Slow-1"
    _write_map(root / "ASD" / "sub-01-zALFFMap.nii")
    _write_map(root / "ASD" / "sub-02-zALFFMap.nii", offset=1)
    _write_map(root / "HC" / "sub-03-zALFFMap.nii", offset=2)
    _write_map(root / "HC" / "sub-04-zALFFMap.nii", offset=3)

    result = inspect_external_metric_directory(root)

    assert result.metric_kind is MetricKind.ALFF
    assert result.scaling is MetricScaling.Z_SCORE
    assert result.group_order == ("ASD", "HC")
    assert [(item.subject_id, item.group) for item in result.images] == [
        ("sub-01", "ASD"),
        ("sub-02", "ASD"),
        ("sub-03", "HC"),
        ("sub-04", "HC"),
    ]
    assert len({item.grid_signature for item in result.images}) == 1
    assert result.coordinate_space == "unknown"
    assert result.blocking_issues == ()
    assert "mask source and version" in result.unknown_processing_fields
    assert len(result.source_tree_hash) == 64


def test_external_metric_inspection_blocks_grid_mix_duplicate_and_metric_mix(
    tmp_path: Path,
) -> None:
    root = tmp_path / "external"
    _write_map(root / "ASD" / "same-zALFFMap.nii")
    _write_map(root / "HC" / "same-zReHoMap.nii", offset=1)
    _write_map(root / "HC" / "other-zReHoMap.nii", offset=2)
    different_grid = np.diag([2.0, 1.0, 1.0, 1.0])
    nib.save(
        nib.Nifti1Image(np.ones((4, 5, 6), dtype=np.float32), different_grid),
        str(root / "ASD" / "other-zALFFMap.nii"),
    )

    result = inspect_external_metric_directory(root)

    assert any("重复" in issue for issue in result.blocking_issues)
    assert any("混有不同指标类型或缩放版本" in issue for issue in result.blocking_issues)
    assert any("空间网格或仿射不一致" in issue for issue in result.blocking_issues)
    assert result.metric_kind is None
    assert result.scaling is None


def test_external_metric_inspection_does_not_infer_unlabeled_group(tmp_path: Path) -> None:
    root = tmp_path / "external"
    _write_map(root / "sub-01-zALFFMap.nii")

    result = inspect_external_metric_directory(root)

    assert result.images == ()
    assert any("至少两个一级组目录" in issue for issue in result.blocking_issues)
    assert any("未找到可读取" in issue for issue in result.blocking_issues)


def test_external_mask_is_hash_bound_to_the_metric_grid(tmp_path: Path) -> None:
    root = tmp_path / "external"
    _write_map(root / "ASD" / "sub-01-zALFFMap.nii")
    _write_map(root / "HC" / "sub-02-zALFFMap.nii")
    _write_map(root / "Mask.nii")

    inspection = inspect_external_metric_directory(root)
    digest, size, grid = inspect_external_mask(root, "Mask.nii")

    assert inspection.blocking_issues == ()
    assert len(digest) == 64
    assert size > 0
    assert grid == inspection.grid_signature


def test_external_import_qualifies_reused_ids_with_the_site_prefix(tmp_path: Path) -> None:
    root = tmp_path / "external"
    _write_map(root / "adolescent" / "07-NYU-zALFFMap_sub011.nii")
    _write_map(root / "adolescent" / "08-UCL-zALFFMap_sub011.nii", offset=1)
    _write_map(root / "children" / "08-UCL-zALFFMap_sub013.nii", offset=2)

    result = inspect_external_metric_directory(root)

    assert result.blocking_issues == ()
    assert {item.subject_id for item in result.images} == {
        "07-NYU-sub011",
        "08-UCL-sub011",
        "08-UCL-sub013",
    }


def test_external_import_blocks_uninspected_deep_directories(tmp_path: Path) -> None:
    root = tmp_path / "external"
    _write_map(root / "ASD" / "sub-01-zALFFMap.nii")
    _write_map(root / "HC" / "sub-02-zALFFMap.nii")
    _write_map(root / "ASD" / "a" / "b" / "c" / "d" / "sub-03-zALFFMap.nii")

    result = inspect_external_metric_directory(root)

    assert len(result.images) == 2
    assert any("无法确认完整输入" in issue for issue in result.blocking_issues)


def test_external_import_never_walks_linked_directories(tmp_path: Path) -> None:
    root = tmp_path / "external"
    _write_map(root / "ASD" / "sub-01-zALFFMap.nii")
    _write_map(root / "HC" / "sub-02-zALFFMap.nii")
    outside = tmp_path / "outside"
    _write_map(outside / "sub-03-zALFFMap.nii")
    try:
        (root / "ASD" / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    result = inspect_external_metric_directory(root)

    assert len(result.images) == 2
    assert any("链接文件或目录" in issue for issue in result.blocking_issues)
