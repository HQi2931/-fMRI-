"""Read-only inspection of external DPABI subject-level metric directories."""

# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import cast

import nibabel as nib
import numpy as np
from nibabel.spatialimages import SpatialImage

from neuroagent.domain.fmri.artifacts import MetricScaling
from neuroagent.domain.fmri.external_import import (
    ExternalMetricImage,
    ExternalMetricInspection,
    ExternalMetricLayout,
)
from neuroagent.domain.fmri.metrics import MetricKind

_MAX_IMAGES = 5_000
_MAX_TOTAL_BYTES = 50 * 1024 * 1024 * 1024
_MAX_DEPTH_FROM_GROUP = 4
_NIFTI_SUFFIXES = (".nii", ".nii.gz")
_UNKNOWN_PROCESSING = (
    "TR and frequency band",
    "temporal filtering and nuisance regression",
    "smoothing and scrubbing",
    "mask source and version",
    "preprocessing software and version",
)


def inspect_external_metric_directory(
    source: Path, *, layout: ExternalMetricLayout = "grouped"
) -> ExternalMetricInspection:
    """Hash and compare subject-level NIfTI files grouped by first-level folders.

    This scanner is deliberately conservative. It does not mark the image space
    or processing history as known, and a directory with ambiguous metrics,
    duplicate subjects, malformed images, or differing grids cannot be included.
    """

    if _is_link(source):
        raise ValueError("external metric source cannot be a symbolic link or junction")
    root = source.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("external metric source must be a directory")
    if _is_link(root):
        raise ValueError("external metric source cannot be a symbolic link or junction")

    issues: list[str] = []
    warnings: list[str] = [
        "导入目录不是本系统生成的运行；处理谱系需按未知项保留。",
        "文件名推断的指标类型和缩放仅作候选，纳入前必须由研究者复核。",
    ]
    root_entries = list(root.iterdir())
    issues.extend(
        f"发现一级链接目录或文件，已跳过：{entry.name}" for entry in root_entries if _is_link(entry)
    )
    group_dirs = sorted(
        (entry for entry in root_entries if entry.is_dir() and not _is_link(entry)),
        key=lambda entry: entry.name.casefold(),
    )
    if len(group_dirs) < 2:
        issues.append("需要至少两个一级组目录；不会从目录顺序推断分组。")

    images: list[ExternalMetricImage] = []
    subject_keys: set[str] = set()
    signatures: set[str] = set()
    metric_pairs: set[tuple[MetricKind, MetricScaling]] = set()
    total_bytes = 0
    seen_files = 0
    tree_entries: list[tuple[str, str, int]] = []
    groups: list[str] = [entry.name for entry in group_dirs]

    for group_dir in group_dirs:
        candidate_paths, linked_paths, deep_paths = _walk_group(
            group_dir, max_depth=_MAX_DEPTH_FROM_GROUP
        )
        issues.extend(
            f"发现链接文件或目录，已跳过：{path.relative_to(root).as_posix()}"
            for path in linked_paths
        )
        issues.extend(
            f"目录超出扫描深度，无法确认完整输入：{path.relative_to(root).as_posix()}"
            for path in deep_paths
        )
        for path in candidate_paths:
            if not path.name.lower().endswith(_NIFTI_SUFFIXES):
                continue
            if _is_link(path):
                issues.append(f"发现链接文件，已拒绝：{path.relative_to(root).as_posix()}")
                continue
            seen_files += 1
            if seen_files > _MAX_IMAGES:
                issues.append(f"影像数量超过安全扫描上限 {_MAX_IMAGES}。")
                break
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
            total_bytes += size
            if total_bytes > _MAX_TOTAL_BYTES:
                issues.append("影像总量超过安全扫描上限 50 GiB。")
                break

            try:
                digest = _sha256(path)
            except OSError as exc:
                issues.append(f"文件无法稳定读取 {relative}: {type(exc).__name__}")
                continue
            tree_entries.append((relative, digest, size))
            inferred = _infer_metric(path.name)
            if inferred is None:
                issues.append(f"无法从文件名确认 ALFF/fALFF/ReHo 及缩放：{relative}")
                continue
            metric_kind, scaling = inferred
            metric_pairs.add((metric_kind, scaling))

            subject_id = _subject_id(path.name)
            key = subject_id.casefold() + (
                f"\0{group_dir.name.casefold()}" if layout == "paired_conditions" else ""
            )
            if key in subject_keys:
                issues.append(f"受试者标识重复（组内或跨组）：{subject_id}")
            subject_keys.add(key)

            try:
                image = cast(SpatialImage, nib.load(str(path), mmap=True))
                if len(image.shape) != 3:
                    raise ValueError(f"需要 3D 指标图，检测到维度 {image.shape}")
                if any(int(size) <= 0 for size in image.shape):
                    raise ValueError("NIfTI 维度必须为正数")
                affine = np.asarray(image.affine, dtype=np.float64)
                if affine.shape != (4, 4) or not np.isfinite(affine).all():
                    raise ValueError("NIfTI 仿射矩阵缺失或包含非有限值")
                if abs(float(np.linalg.det(affine[:3, :3]))) < 1e-12:
                    raise ValueError("NIfTI 仿射矩阵不可逆")
                # Read bounded z slabs to check every voxel without materializing
                # all subjects or all 3D maps at once.
                proxy = image.dataobj
                for z_start in range(0, int(image.shape[2]), 16):
                    if not np.isfinite(np.asanyarray(proxy[:, :, z_start : z_start + 16])).all():
                        raise ValueError("NIfTI 包含 NaN 或 Inf 体素")
                affine_rounded = np.round(affine, 6)
                signature_input = {
                    "shape_ijk": tuple(int(item) for item in image.shape),
                    "affine": affine_rounded.tolist(),
                }
                signature = hashlib.sha256(
                    json.dumps(signature_input, sort_keys=True, separators=(",", ":")).encode(
                        "utf-8"
                    )
                ).hexdigest()
                signatures.add(signature)
                images.append(
                    ExternalMetricImage(
                        subject_id=subject_id,
                        group=group_dir.name,
                        condition=group_dir.name if layout == "paired_conditions" else None,
                        relative_path=relative,
                        sha256=digest,
                        size_bytes=size,
                        shape_ijk=tuple(int(item) for item in image.shape),
                        affine=tuple(
                            tuple(float(value) for value in row) for row in affine_rounded
                        ),
                        grid_signature=signature,
                        metric_kind=metric_kind,
                        scaling=scaling,
                    )
                )
            except (OSError, ValueError, TypeError, nib.filebasedimages.ImageFileError) as exc:
                issues.append(f"NIfTI 检查失败 {relative}: {type(exc).__name__}: {exc}")

        if seen_files > _MAX_IMAGES or total_bytes > _MAX_TOTAL_BYTES:
            break

    if not images:
        issues.append("未找到可读取的受试者级指标 NIfTI。")
    if len(metric_pairs) != 1:
        if metric_pairs:
            issues.append("导入目录混有不同指标类型或缩放版本。")
        else:
            issues.append("未能确定统一的指标类型和缩放版本。")
    if len(signatures) > 1:
        issues.append("指标图的空间网格或仿射不一致，已阻止纳入。")
    if layout == "paired_conditions":
        if len(groups) != 2:
            issues.append("配对布局必须恰好包含两个一级条件目录。")
        else:
            subject_sets = [
                {image.subject_id for image in images if image.condition == condition}
                for condition in groups
            ]
            if subject_sets[0] != subject_sets[1]:
                issues.append("两个条件的受试者集合不一致，存在缺失配对。")
    group_keys = {item.casefold() for item in groups}
    if images and any(image.group.casefold() not in group_keys for image in images):
        issues.append("部分指标图没有明确的一级组标签。")

    tree_hash = hashlib.sha256(
        json.dumps(sorted(tree_entries), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    result_metric_kind: MetricKind | None = None
    result_scaling: MetricScaling | None = None
    if len(metric_pairs) == 1:
        result_metric_kind, result_scaling = next(iter(metric_pairs))
    return ExternalMetricInspection(
        layout=layout,
        source_tree_hash=tree_hash,
        metric_kind=result_metric_kind,
        scaling=result_scaling,
        images=tuple(
            sorted(images, key=lambda item: (item.group.casefold(), item.subject_id.casefold()))
        ),
        group_order=tuple(groups),
        grid_signature=next(iter(signatures)) if len(signatures) == 1 else None,
        blocking_issues=tuple(dict.fromkeys(issues)),
        warnings=tuple(warnings),
        unknown_processing_fields=_UNKNOWN_PROCESSING,
    )


def inspect_external_mask(source: Path, relative_path: str) -> tuple[str, int, str]:
    """Validate a researcher-selected binary mask against an inspected metric grid."""

    root = source.resolve(strict=True)
    relative = Path(relative_path)
    if (
        not relative_path
        or relative.is_absolute()
        or ".." in relative.parts
        or "\\" in relative_path
        or not relative_path.lower().endswith(_NIFTI_SUFFIXES)
    ):
        raise ValueError("mask path must be a relative NIfTI path inside the import root")
    candidate = root / relative
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if _is_link(cursor):
            raise ValueError("external mask cannot traverse a symbolic link or junction")
    resolved = candidate.resolve(strict=True)
    if _is_link(resolved):
        raise ValueError("external mask cannot be a symbolic link or junction")
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("external mask path escaped the import root") from exc
    if not resolved.is_file():
        raise ValueError("external mask must be a NIfTI file")

    digest = _sha256(resolved)
    image = cast(SpatialImage, nib.load(str(resolved), mmap=True))
    if len(image.shape) != 3 or any(int(size) <= 0 for size in image.shape):
        raise ValueError("external mask must be a non-empty 3D NIfTI")
    affine = np.asarray(image.affine, dtype=np.float64)
    if affine.shape != (4, 4) or not np.isfinite(affine).all():
        raise ValueError("external mask affine is missing or non-finite")
    if abs(float(np.linalg.det(affine[:3, :3]))) < 1e-12:
        raise ValueError("external mask affine is not invertible")
    nonzero = False
    proxy = image.dataobj
    for z_start in range(0, int(image.shape[2]), 16):
        slab = np.asanyarray(proxy[:, :, z_start : z_start + 16])
        if not np.isfinite(slab).all():
            raise ValueError("external mask contains NaN or Inf voxels")
        nonzero = nonzero or bool(np.any(slab > 0))
    if not nonzero:
        raise ValueError("external mask contains no positive voxels")
    signature_input = {
        "shape_ijk": tuple(int(item) for item in image.shape),
        "affine": np.round(affine, 6).tolist(),
    }
    signature = hashlib.sha256(
        json.dumps(signature_input, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest, resolved.stat().st_size, signature


def _walk_group(group_dir: Path, *, max_depth: int) -> tuple[list[Path], list[Path], list[Path]]:
    paths: list[Path] = []
    linked: list[Path] = []
    deep: list[Path] = []
    pending = [(group_dir, 0)]
    while pending:
        folder, depth = pending.pop()
        for path in folder.iterdir():
            if _is_link(path):
                linked.append(path)
                continue
            if path.is_dir():
                if depth + 1 >= max_depth:
                    deep.append(path)
                else:
                    pending.append((path, depth + 1))
            elif path.is_file():
                paths.append(path)
    return (
        sorted(paths, key=lambda item: item.as_posix().casefold()),
        sorted(linked, key=lambda item: item.as_posix().casefold()),
        sorted(deep, key=lambda item: item.as_posix().casefold()),
    )


def _is_link(path: Path) -> bool:
    return path.is_symlink() or _is_junction(path)


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker()) if checker is not None else False


def _sha256(path: Path) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("file changed during hashing")
    return digest.hexdigest()


def _nifti_stem(name: str) -> str:
    return name[:-7] if name.lower().endswith(".nii.gz") else name[:-4]


def _subject_id(name: str) -> str:
    """Remove DPABI metric markers and preserve site-qualified subject IDs.

    DPABI cohort exports often reset IDs such as ``sub011`` for each acquisition
    site. Keeping the filename's site prefix avoids incorrectly merging distinct
    participants, while a pure suffix (for example ``zALFFMap_29225``) remains
    the subject ID when no prefix exists.
    """

    stem = _nifti_stem(name)
    marker = re.search(r"(?:zf?alff|f?alff|z?reho)map", stem, flags=re.IGNORECASE)
    if marker is None:
        return stem
    prefix = stem[: marker.start()].rstrip("_- ")
    suffix = stem[marker.end() :].strip("_- ")
    if prefix and suffix:
        return f"{prefix}-{suffix}"
    return suffix or prefix or stem


def _infer_metric(filename: str) -> tuple[MetricKind, MetricScaling] | None:
    normalized = re.sub(r"[^a-z]", "", filename.casefold())
    if "zfalffmap" in normalized:
        return MetricKind.FALFF, MetricScaling.Z_SCORE
    if "falffmap" in normalized:
        return MetricKind.FALFF, MetricScaling.RAW
    if "zalffmap" in normalized:
        return MetricKind.ALFF, MetricScaling.Z_SCORE
    if "alffmap" in normalized:
        return MetricKind.ALFF, MetricScaling.RAW
    if "zreho" in normalized:
        return MetricKind.REHO, MetricScaling.Z_SCORE
    if "reho" in normalized:
        return MetricKind.REHO, MetricScaling.RAW
    return None
