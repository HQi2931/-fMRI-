"""Bounded, integrity-checked access to registered local run artifacts."""

from __future__ import annotations

import gzip
import hashlib
import math
import struct
import zlib
from pathlib import Path, PurePosixPath

from neuroagent.application.contracts.projects import ProjectView
from neuroagent.application.contracts.runs import (
    ArtifactView,
    RunView,
)
from neuroagent.application.errors import InputValidationError


def artifact_content_path(artifact: ArtifactView, run: RunView, project: ProjectView) -> Path:
    if (
        artifact.run_id != run.run_id
        or artifact.project_id != project.project_id
        or run.project_id != project.project_id
    ):
        raise InputValidationError("artifact_scope_invalid", "产物与运行范围不匹配。")
    relative = PurePosixPath(artifact.relative_path)
    if (
        relative.is_absolute()
        or any(part in {"..", "."} for part in relative.parts)
        or relative.as_posix() != artifact.relative_path
        or "\\" in artifact.relative_path
        or ":" in artifact.relative_path
        or not relative.parts
    ):
        raise InputValidationError("artifact_path_invalid", "产物路径无效。")
    root = Path(project.work_root) / run.run_id
    candidate = root.joinpath(*relative.parts)
    try:
        # Refuse links at every level, including the run root and junctions.
        for entry in (root, *candidate.parents, candidate):
            if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
                raise InputValidationError("artifact_path_invalid", "产物路径包含链接。")
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
            raise InputValidationError("artifact_path_invalid", "产物路径超出运行范围。")
        if resolved.stat().st_size != artifact.size_bytes:
            raise InputValidationError(
                "artifact_integrity_changed", "产物大小已改变。无法下载或预览。"
            )
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != artifact.checksum:
            raise InputValidationError(
                "artifact_integrity_changed", "产物内容已改变。无法下载或预览。"
            )
        return resolved
    except OSError as exc:
        raise InputValidationError("artifact_unavailable", "注册产物文件不可用。") from exc


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def nifti_preview_slice_count(path: Path) -> int:
    """Read a bounded NIfTI-1 preview's axial slice count from its header."""
    if not path.name.lower().endswith((".nii", ".nii.gz")):
        raise InputValidationError("artifact_preview_unsupported", "此产物不支持 NIfTI 切片预览。")
    opener = gzip.open if path.name.lower().endswith(".gz") else Path.open
    try:
        with opener(path, "rb") as stream:
            header = stream.read(352)
        if len(header) != 352:
            raise ValueError("short header")
        endian = "<" if struct.unpack("<i", header[:4])[0] == 348 else ">"
        if struct.unpack(endian + "i", header[:4])[0] != 348 or header[344:348] != b"n+1\0":
            raise ValueError("not single-file NIfTI-1")
        dimensions = struct.unpack_from(endian + "8h", header, 40)
        width, height, depth = dimensions[1:4]
        if dimensions[0] not in {3, 4} or not all(
            0 < size <= 512 for size in (width, height, depth)
        ):
            raise ValueError("unsupported dimensions")
        return int(depth)
    except (OSError, EOFError, ValueError, struct.error, OverflowError) as exc:
        raise InputValidationError(
            "artifact_preview_invalid", "无法读取受限 NIfTI 预览信息。"
        ) from exc


def nifti_preview_png(path: Path, *, slice_index: int | None = None) -> bytes:
    """Render the central voxel-z slice of the first volume, without resampling.

    This inspection image is deliberately not labelled radiological/MNI.  The
    affine is not applied and the preview does not replace scientific QC.
    """
    if not path.name.lower().endswith((".nii", ".nii.gz")):
        raise InputValidationError("artifact_preview_unsupported", "此产物不支持 NIfTI 切片预览。")
    opener = gzip.open if path.name.lower().endswith(".gz") else Path.open
    try:
        with opener(path, "rb") as stream:
            header = stream.read(352)
            if len(header) != 352:
                raise ValueError("short header")
            endian = "<" if struct.unpack("<i", header[:4])[0] == 348 else ">"
            if struct.unpack(endian + "i", header[:4])[0] != 348 or header[344:348] != b"n+1\0":
                raise ValueError("not single-file NIfTI-1")
            dimensions = struct.unpack_from(endian + "8h", header, 40)
            width, height, depth = dimensions[1:4]
            if dimensions[0] not in {3, 4} or not all(
                0 < size <= 512 for size in (width, height, depth)
            ):
                raise ValueError("unsupported dimensions")
            z_index = depth // 2 if slice_index is None else slice_index
            if (
                isinstance(z_index, bool)
                or not isinstance(z_index, int)
                or not 0 <= z_index < depth
            ):
                raise InputValidationError(
                    "artifact_preview_slice_invalid", "切片序号超出 NIfTI 图像范围。"
                )
            formats = {2: "B", 4: "h", 8: "i", 16: "f", 64: "d", 256: "b", 512: "H", 768: "I"}
            datatype, bitpix = struct.unpack_from(endian + "2h", header, 70)
            code = formats.get(datatype)
            if code is None or struct.calcsize(code) * 8 != bitpix:
                raise ValueError("unsupported voxel type")
            offset = struct.unpack_from(endian + "f", header, 108)[0]
            if not math.isfinite(offset) or offset < 352 or offset > 1024 * 1024:
                raise ValueError("unsupported data offset")
            count = width * height
            slice_offset = int(offset) + z_index * count * (bitpix // 8)
            if slice_offset > 64 * 1024 * 1024:
                raise ValueError("preview read budget exceeded")
            stream.seek(slice_offset)
            payload = stream.read(count * (bitpix // 8))
            if len(payload) != count * (bitpix // 8):
                raise ValueError("truncated voxel data")
            values = [item[0] for item in struct.iter_unpack(endian + code, payload)]
            slope, intercept = struct.unpack_from(endian + "2f", header, 112)
            if slope and math.isfinite(slope) and math.isfinite(intercept):
                values = [value * slope + intercept for value in values]
            finite = sorted(value for value in values if math.isfinite(value))
            if not finite:
                raise ValueError("no finite voxels")
            lower = finite[int((len(finite) - 1) * 0.02)]
            upper = finite[int((len(finite) - 1) * 0.98)]
            span = upper - lower
            pixels = bytes(
                min(255, max(0, round((value - lower) / span * 255)))
                if math.isfinite(value) and span > 0
                else 0
                for value in values
            )
            rows = b"".join(
                b"\0" + pixels[index : index + width] for index in range(0, count, width)
            )
            return (
                b"\x89PNG\r\n\x1a\n"
                + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
                + _png_chunk(b"IDAT", zlib.compress(rows))
                + _png_chunk(b"IEND", b"")
            )
    except (OSError, EOFError, ValueError, struct.error, OverflowError) as exc:
        raise InputValidationError(
            "artifact_preview_invalid", "无法生成受限 NIfTI 预览。请下载产物检查。"
        ) from exc
