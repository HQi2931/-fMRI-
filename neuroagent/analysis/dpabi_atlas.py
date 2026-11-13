"""Read-only peak and cluster localization against configured DPABI atlases."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import nibabel as nib
import numpy as np
from scipy import ndimage  # type: ignore[import-untyped]
from scipy.io import loadmat  # type: ignore[import-untyped]

from neuroagent.domain.fmri.results import ClusterRecord


@dataclass(frozen=True)
class DpabiAtlas:
    atlas_id: str
    name: str
    space: str
    nifti_filename: str
    labels_filename: str


@dataclass(frozen=True)
class AtlasAvailability:
    atlas_id: str
    name: str
    space: str
    available: bool
    voxel_size_mm: tuple[float, float, float] | None
    label_count: int | None
    content_hash: str | None
    reason: str | None


@dataclass(frozen=True)
class AtlasPeakSample:
    atlas_id: str
    atlas_name: str
    label_value: int | None
    label: str | None
    voxel_ijk: tuple[int, int, int] | None
    voxel_center_mm: tuple[float, float, float] | None
    distance_mm: float | None


@dataclass(frozen=True)
class AtlasRegionCoverage:
    label_value: int
    label: str
    voxel_count: int
    proportion: float


@dataclass(frozen=True)
class AtlasClusterLocalization:
    cluster_id: str
    atlas_peak: AtlasPeakSample
    coverage: tuple[AtlasRegionCoverage, ...]


DPABI_ATLASES = (
    DpabiAtlas("aal", "AAL", "MNI", "aal.nii", "aal_Labels.mat"),
    DpabiAtlas(
        "aal3",
        "AAL3",
        "MNI",
        "AAL3v1_1mm.nii",
        "AAL3v1_1mm_Labels.mat",
    ),
    DpabiAtlas(
        "brodmann",
        "Brodmann areas",
        "MNI",
        "Brodmann_YCG.nii",
        "Brodmann_YCG_Labels.mat",
    ),
)


def list_dpabi_atlases(dpabi_dir: Path | None) -> tuple[AtlasAvailability, ...]:
    """Return availability for the supported DPABI-bundled atlas files."""

    output: list[AtlasAvailability] = []
    for atlas in DPABI_ATLASES:
        paths = _atlas_paths(dpabi_dir, atlas)
        if paths is None:
            output.append(
                AtlasAvailability(
                    atlas.atlas_id,
                    atlas.name,
                    atlas.space,
                    False,
                    None,
                    None,
                    None,
                    "DPABI 未配置或其 Templates 图谱文件缺失。",
                )
            )
            continue
        nifti_path, labels_path = paths
        try:
            image = _load_nifti(nifti_path)
            if len(image.shape) != 3:
                raise ValueError("图谱必须是三维离散标签图。")
            affine = _valid_affine(image.affine)
            if image.get_data_dtype().kind not in "iu":
                raise ValueError("图谱 NIfTI 不是整数标签图。")
            labels = _read_labels(labels_path)
            raw_zooms = image.header.get_zooms()
            zooms = (float(raw_zooms[0]), float(raw_zooms[1]), float(raw_zooms[2]))
            output.append(
                AtlasAvailability(
                    atlas.atlas_id,
                    atlas.name,
                    atlas.space,
                    True,
                    zooms,
                    len(labels),
                    _content_hash(paths),
                    None,
                )
            )
            del affine
        except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
            output.append(
                AtlasAvailability(
                    atlas.atlas_id,
                    atlas.name,
                    atlas.space,
                    False,
                    None,
                    None,
                    None,
                    f"图谱文件无法验证：{type(exc).__name__}。",  # noqa: RUF001
                )
            )
    return tuple(output)


def sample_atlas_peak(
    *,
    dpabi_dir: Path | None,
    atlas_id: str,
    coordinate_mm: tuple[float, float, float],
) -> AtlasPeakSample:
    """Sample one world-space peak using the atlas inverse affine and nearest voxel."""

    atlas = next((item for item in DPABI_ATLASES if item.atlas_id == atlas_id), None)
    if atlas is None:
        raise ValueError("unsupported DPABI atlas")
    paths = _atlas_paths(dpabi_dir, atlas)
    if paths is None:
        raise ValueError("DPABI atlas files are unavailable")
    nifti_path, labels_path = paths
    image = _load_nifti(nifti_path)
    if len(image.shape) != 3 or image.get_data_dtype().kind not in "iu":
        raise ValueError("DPABI atlas must be a 3D integer-label NIfTI")
    affine = _valid_affine(image.affine)
    inverse = np.linalg.inv(affine)
    world = np.asarray((*coordinate_mm, 1.0), dtype=float)
    voxel_float = (inverse @ world)[:3]
    voxel = np.rint(voxel_float).astype(int)
    if np.any(voxel < 0) or np.any(voxel >= np.asarray(image.shape[:3])):
        return AtlasPeakSample(atlas.atlas_id, atlas.name, None, None, None, None, None)

    index = (int(voxel[0]), int(voxel[1]), int(voxel[2]))
    label_value = int(image.dataobj[index])
    voxel_center = (affine @ np.asarray((*index, 1.0), dtype=float))[:3]
    distance = float(np.linalg.norm(voxel_center - np.asarray(coordinate_mm, dtype=float)))
    label_name = _read_labels(labels_path).get(label_value)
    return AtlasPeakSample(
        atlas.atlas_id,
        atlas.name,
        label_value,
        label_name,
        index,
        (float(voxel_center[0]), float(voxel_center[1]), float(voxel_center[2])),
        distance,
    )


def get_atlas(dpabi_dir: Path | None, atlas_id: str) -> tuple[DpabiAtlas, str]:
    """Resolve a supported atlas and return a digest of its exact DPABI files."""

    atlas = next((item for item in DPABI_ATLASES if item.atlas_id == atlas_id), None)
    if atlas is None:
        raise ValueError("unsupported DPABI atlas")
    paths = _atlas_paths(dpabi_dir, atlas)
    if paths is None:
        raise ValueError("DPABI atlas files are unavailable")
    return atlas, _content_hash(paths)


def verify_peak_against_statistical_map(
    *,
    map_path: Path,
    coordinate_mm: tuple[float, float, float],
    expected_statistic: float,
) -> None:
    """Verify the registered peak coordinate resolves to its value in the source map."""

    image = _load_nifti(map_path)
    if len(image.shape) != 3:
        raise ValueError("统计来源图必须是三维 NIfTI。")
    affine = _valid_affine(image.affine)
    voxel_float = (np.linalg.inv(affine) @ np.asarray((*coordinate_mm, 1.0)))[:3]
    voxel = np.rint(voxel_float).astype(int)
    if np.any(voxel < 0) or np.any(voxel >= np.asarray(image.shape[:3])):
        raise ValueError("簇峰值坐标超出统计来源图网格。")
    index = tuple(int(value) for value in voxel)
    observed = float(image.dataobj[index])
    tolerance = max(1e-5, abs(expected_statistic) * 1e-4)
    if not np.isfinite(observed) or abs(observed - expected_statistic) > tolerance:
        raise ValueError("簇表峰值与统计来源图的仿射定位或数值不一致。")


def peak_axial_slice(*, map_path: Path, coordinate_mm: tuple[float, float, float]) -> int:
    """Resolve a world coordinate to its axial slice in a statistical map."""

    image = _load_nifti(map_path)
    if len(image.shape) != 3:
        raise ValueError("统计来源图必须是三维 NIfTI。")
    affine = _valid_affine(image.affine)
    voxel = np.rint(
        (np.linalg.inv(affine) @ np.asarray((*coordinate_mm, 1.0), dtype=float))[:3]
    ).astype(int)
    if np.any(voxel < 0) or np.any(voxel >= np.asarray(image.shape[:3])):
        raise ValueError("簇峰值坐标超出统计来源图网格。")
    return int(voxel[2])


def localize_cluster_map(
    *,
    dpabi_dir: Path | None,
    atlas_id: str,
    map_path: Path,
    clusters: tuple[ClusterRecord, ...],
) -> tuple[AtlasClusterLocalization, ...]:
    """Validate cluster extents and measure their voxel overlap with atlas labels."""

    if not clusters:
        return ()
    atlas = next((item for item in DPABI_ATLASES if item.atlas_id == atlas_id), None)
    if atlas is None:
        raise ValueError("unsupported DPABI atlas")
    paths = _atlas_paths(dpabi_dir, atlas)
    if paths is None:
        raise ValueError("DPABI atlas files are unavailable")
    atlas_path, labels_path = paths
    atlas_image = _load_nifti(atlas_path)
    map_image = _load_nifti(map_path)
    if (
        len(atlas_image.shape) != 3
        or atlas_image.get_data_dtype().kind not in "iu"
        or len(map_image.shape) != 3
        or map_image.get_data_dtype().kind not in "fi"
        or int(np.prod(map_image.shape)) > 16_000_000
    ):
        raise ValueError("图谱或统计图维度不受支持，或体素数量超过定位上限。")  # noqa: RUF001
    atlas_affine = _valid_affine(atlas_image.affine)
    map_affine = _valid_affine(map_image.affine)
    map_data = np.asarray(map_image.dataobj, dtype=np.float32)
    if not np.isfinite(map_data).any():
        raise ValueError("统计图没有有限体素。")
    atlas_data = np.asarray(atlas_image.dataobj)
    if atlas_data.ndim != 3 or not np.isfinite(atlas_data).all():
        raise ValueError("DPABI 图谱包含无效标签体素。")
    label_names = _read_labels(labels_path)
    index_transform = np.linalg.inv(atlas_affine) @ map_affine
    map_inverse = np.linalg.inv(map_affine)
    shape = np.asarray(map_image.shape[:3])
    structure = np.ones((3, 3, 3), dtype=np.uint8)
    components_by_direction: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    output: list[AtlasClusterLocalization] = []

    for cluster in clusters:
        direction = cluster.direction or (1 if cluster.peak_statistic > 0 else -1)
        if direction not in {-1, 1} or (cluster.peak_statistic > 0) != (direction > 0):
            raise ValueError("簇表方向与峰值统计量符号不一致。")
        if direction not in components_by_direction:
            mask = np.isfinite(map_data) & (direction * map_data > 0)
            component_labels, _ = ndimage.label(mask, structure=structure)
            component_sizes = np.bincount(component_labels.ravel())
            components_by_direction[direction] = (component_labels, component_sizes)

        coordinate = np.asarray((*cluster.peak_coordinate_mm, 1.0), dtype=float)
        map_voxel = np.rint((map_inverse @ coordinate)[:3]).astype(int)
        if np.any(map_voxel < 0) or np.any(map_voxel >= shape):
            raise ValueError("簇峰值坐标超出统计来源图网格。")
        map_index = (int(map_voxel[0]), int(map_voxel[1]), int(map_voxel[2]))
        observed = float(map_data[map_index])
        tolerance = max(1e-5, abs(cluster.peak_statistic) * 1e-4)
        if not np.isfinite(observed) or abs(observed - cluster.peak_statistic) > tolerance:
            raise ValueError("簇表峰值与统计来源图的仿射定位或数值不一致。")
        component_labels, component_sizes = components_by_direction[direction]
        component_id = int(component_labels[map_index])
        if component_id == 0 or int(component_sizes[component_id]) != cluster.extent_voxels:
            raise ValueError("簇表范围与统计图 26 邻域连通域不一致。")

        peak_voxel = np.rint((np.linalg.inv(atlas_affine) @ coordinate)[:3]).astype(int)
        if np.any(peak_voxel < 0) or np.any(peak_voxel >= np.asarray(atlas_image.shape[:3])):
            peak_sample = AtlasPeakSample(atlas_id, atlas.name, None, None, None, None, None)
        else:
            peak_index = (int(peak_voxel[0]), int(peak_voxel[1]), int(peak_voxel[2]))
            label_value = int(atlas_data[peak_index])
            center = (atlas_affine @ np.asarray((*peak_index, 1.0)))[:3]
            peak_sample = AtlasPeakSample(
                atlas_id,
                atlas.name,
                label_value,
                label_names.get(label_value),
                peak_index,
                (float(center[0]), float(center[1]), float(center[2])),
                float(np.linalg.norm(center - coordinate[:3])),
            )

        coverage_counts: dict[int, int] = {}
        flat_labels = component_labels.ravel()
        chunk_voxels = 250_000
        for start in range(0, flat_labels.size, chunk_voxels):
            stop = min(start + chunk_voxels, flat_labels.size)
            offsets = np.flatnonzero(flat_labels[start:stop] == component_id) + start
            if offsets.size == 0:
                continue
            i, j, k = np.unravel_index(offsets, map_image.shape[:3])
            homogeneous = np.column_stack((i, j, k, np.ones(offsets.size, dtype=float)))
            atlas_voxels = np.rint((index_transform @ homogeneous.T).T[:, :3]).astype(int)
            inside = np.all(atlas_voxels >= 0, axis=1) & np.all(
                atlas_voxels < np.asarray(atlas_image.shape[:3]), axis=1
            )
            sampled_labels = np.zeros(offsets.size, dtype=np.int64)
            inside_voxels = atlas_voxels[inside]
            if inside_voxels.size:
                sampled_labels[inside] = atlas_data[
                    inside_voxels[:, 0], inside_voxels[:, 1], inside_voxels[:, 2]
                ].astype(np.int64)
            values, counts = np.unique(sampled_labels, return_counts=True)
            for value, count in zip(values, counts, strict=True):
                key = int(value)
                coverage_counts[key] = coverage_counts.get(key, 0) + int(count)

        total = cluster.extent_voxels
        if sum(coverage_counts.values()) != total:
            raise ValueError("簇脑区覆盖统计的体素总数不一致。")
        coverage = tuple(
            AtlasRegionCoverage(
                label_value=value,
                label=label_names.get(value, "背景或图谱外" if value == 0 else "未知图谱标签"),
                voxel_count=count,
                proportion=count / total,
            )
            for value, count in sorted(
                coverage_counts.items(), key=lambda item: (-item[1], item[0])
            )
        )
        output.append(AtlasClusterLocalization(cluster.cluster_id, peak_sample, coverage))
    return tuple(output)


def cluster_voxel_mask(map_path: Path, cluster: ClusterRecord) -> np.ndarray:
    """Reconstruct one signed 26-neighbour component from its registered peak."""
    image = _load_nifti(map_path)
    if len(image.shape) != 3 or int(np.prod(image.shape)) > 16_000_000:
        raise ValueError("statistical map exceeds the supported ROI grid")
    affine = _valid_affine(image.affine)
    values = np.asarray(image.dataobj, dtype=np.float32)
    direction = cluster.direction or (1 if cluster.peak_statistic > 0 else -1)
    if direction not in {-1, 1} or (cluster.peak_statistic > 0) != (direction > 0):
        raise ValueError("cluster direction disagrees with its peak")
    coordinate = np.asarray((*cluster.peak_coordinate_mm, 1.0))
    voxel = np.rint((np.linalg.inv(affine) @ coordinate)[:3]).astype(int)
    if np.any(voxel < 0) or np.any(voxel >= np.asarray(image.shape)):
        raise ValueError("cluster peak lies outside the statistical map")
    index = tuple(int(value) for value in voxel)
    observed = float(values[index])
    if not np.isfinite(observed) or abs(observed - cluster.peak_statistic) > max(
        1e-5, abs(cluster.peak_statistic) * 1e-4
    ):
        raise ValueError("cluster peak disagrees with the statistical map")
    labels, _ = ndimage.label(
        np.isfinite(values) & (direction * values > 0), structure=np.ones((3, 3, 3), dtype=np.uint8)
    )
    component = int(labels[index])
    mask = labels == component
    if component == 0 or int(mask.sum()) != cluster.extent_voxels:
        raise ValueError("cluster extent disagrees with its 26-neighbour component")
    return np.asarray(mask, dtype=bool)


def _atlas_paths(dpabi_dir: Path | None, atlas: DpabiAtlas) -> tuple[Path, Path] | None:
    if dpabi_dir is None:
        return None
    template_root = dpabi_dir / "Templates"
    if template_root.is_symlink() or (
        hasattr(template_root, "is_junction") and template_root.is_junction()
    ):
        return None
    try:
        root = template_root.resolve(strict=True)
        if not root.is_dir():
            return None
        paths = (root / atlas.nifti_filename, root / atlas.labels_filename)
        for path in paths:
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                return None
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                return None
        return paths
    except OSError:
        return None


def _valid_affine(value: Any) -> np.ndarray:
    affine = np.asarray(value, dtype=float)
    if affine.shape != (4, 4) or not np.isfinite(affine).all():
        raise ValueError("NIfTI 仿射矩阵无效。")
    if abs(float(np.linalg.det(affine[:3, :3]))) < 1e-8:
        raise ValueError("NIfTI 仿射矩阵不可逆。")
    return affine


def _read_labels(path: Path) -> dict[int, str]:
    return _read_labels_cached(str(path), _file_content_hash(path))


@lru_cache(maxsize=8)
def _read_labels_cached(path: str, content_hash: str) -> dict[int, str]:
    del content_hash
    reference = np.asarray(
        loadmat(path, squeeze_me=True, struct_as_record=False)["Reference"], dtype=object
    )
    if reference.ndim != 2 or reference.shape[1] < 2:
        raise ValueError("DPABI 标签表 Reference 格式无效。")
    labels: dict[int, str] = {}
    for row in reference:
        name = _matlab_string(row[0])
        value = int(np.asarray(row[1]).item())
        if value >= 0 and name and name.casefold() not in {"none", "background"}:
            labels[value] = name
    return labels


@lru_cache(maxsize=8)
def _load_nifti_cached(path: str, content_hash: str) -> Any:
    del content_hash
    return nib.load(path)


def _load_nifti(path: Path) -> Any:
    return _load_nifti_cached(str(path), _file_content_hash(path))


def _file_content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_hash(paths: tuple[Path, Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def _matlab_string(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict").strip()
    if isinstance(value, np.ndarray):
        if value.dtype.kind in "US":
            return "".join(str(item) for item in value.reshape(-1)).strip()
        if value.size == 1:
            return _matlab_string(value.item())
    return str(value).strip()
