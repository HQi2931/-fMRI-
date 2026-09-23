"""NIfTI header derivation, including the trap that the file name is not geometry."""

from __future__ import annotations

import gzip
import struct
from pathlib import Path

import pytest

from neuroagent.infrastructure.filesystem.nifti_header import (
    NiftiHeaderError,
    read_nifti_header,
)


def _nifti_bytes(
    *,
    dims: tuple[int, int, int, int],
    pixdim: tuple[float, float, float, float],
    datatype: int = 16,
    endian: str = "<",
    sizeof_hdr: int = 348,
    ndim: int = 4,
) -> bytes:
    header = bytearray(352)
    struct.pack_into(f"{endian}i", header, 0, sizeof_hdr)
    struct.pack_into(f"{endian}8h", header, 40, ndim, dims[0], dims[1], dims[2], dims[3], 1, 1, 1)
    struct.pack_into(f"{endian}h", header, 70, datatype)
    struct.pack_into(
        f"{endian}8f", header, 76, 1.0, pixdim[0], pixdim[1], pixdim[2], pixdim[3], 0.0, 0.0, 0.0
    )
    return bytes(header)


def _write(path: Path, payload: bytes, *, gzipped: bool = False) -> Path:
    if gzipped:
        with gzip.open(path, "wb") as stream:
            stream.write(payload)
    else:
        path.write_bytes(payload)
    return path


def test_reads_dimensions_datatype_and_tr(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "bold.nii",
        _nifti_bytes(dims=(64, 64, 33, 234), pixdim=(3.125, 3.125, 3.6, 2.0)),
    )

    header = read_nifti_header(path)

    assert (header.nx, header.ny, header.nz, header.nt) == (64, 64, 33, 234)
    assert header.volume_count == 234
    assert header.voxel_count == 64 * 64 * 33
    assert header.tr_seconds == pytest.approx(2.0)
    assert header.pixdim_mm == pytest.approx((3.125, 3.125, 3.6))
    assert header.datatype == "float32"
    assert header.little_endian is True


def test_header_wins_over_a_misleading_file_name(tmp_path: Path) -> None:
    """The real trap: a name may say 240 while the header says 234."""

    path = _write(
        tmp_path / "Sub_001_ge_func_31x31x30_240_RS_series_4.nii",
        _nifti_bytes(dims=(64, 64, 33, 234), pixdim=(3.125, 3.125, 3.6, 2.0)),
    )

    header = read_nifti_header(path)

    assert header.volume_count == 234
    assert header.nx == 64
    assert "31x31x30" in path.name  # the name is a scanner descriptor, not geometry


def test_reads_gzipped_and_big_endian_files(tmp_path: Path) -> None:
    payload = _nifti_bytes(dims=(10, 10, 10, 5), pixdim=(2.0, 2.0, 2.0, 1.5))
    gz = _write(tmp_path / "bold.nii.gz", payload, gzipped=True)
    assert read_nifti_header(gz).volume_count == 5

    big = _write(
        tmp_path / "big.nii",
        _nifti_bytes(dims=(10, 10, 10, 5), pixdim=(2.0, 2.0, 2.0, 1.5), endian=">"),
    )
    assert read_nifti_header(big).little_endian is False


def test_three_dimensional_image_is_one_volume(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "t1.nii",
        _nifti_bytes(dims=(256, 256, 176, 1), pixdim=(1.0, 1.0, 1.0, 0.0), ndim=3),
    )

    header = read_nifti_header(path)

    assert header.ndim == 3
    assert header.volume_count == 1


def test_non_nifti_and_truncated_files_fail_closed(tmp_path: Path) -> None:
    junk = tmp_path / "junk.nii"
    # Long enough to carry a header, but the leading int32 is neither 348 nor 540.
    junk.write_bytes(b"\x00" * 400)
    with pytest.raises(NiftiHeaderError, match="not a NIfTI-1 file"):
        read_nifti_header(junk)

    short = tmp_path / "short.nii"
    short.write_bytes(b"\x5c\x01\x00\x00")
    with pytest.raises(NiftiHeaderError, match="truncated"):
        read_nifti_header(short)

    missing = tmp_path / "absent.nii"
    with pytest.raises(NiftiHeaderError, match="cannot read"):
        read_nifti_header(missing)


def test_four_dimensional_image_without_a_tr_is_refused(tmp_path: Path) -> None:
    """A missing TR must fail closed rather than become a guessed default."""

    path = _write(
        tmp_path / "no-tr.nii",
        _nifti_bytes(dims=(64, 64, 33, 200), pixdim=(3.0, 3.0, 3.0, 0.0)),
    )

    with pytest.raises(NiftiHeaderError, match="non-positive TR"):
        read_nifti_header(path)


def test_unsupported_datatype_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "odd.nii",
        _nifti_bytes(dims=(4, 4, 4, 2), pixdim=(1.0, 1.0, 1.0, 2.0), datatype=1234),
    )

    with pytest.raises(NiftiHeaderError, match="unsupported NIfTI datatype"):
        read_nifti_header(path)


def test_implausible_dimensionality_is_refused(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "flat.nii",
        _nifti_bytes(dims=(4, 4, 1, 1), pixdim=(1.0, 1.0, 1.0, 0.0), ndim=2),
    )

    with pytest.raises(NiftiHeaderError, match="only 3D and 4D"):
        read_nifti_header(path)
