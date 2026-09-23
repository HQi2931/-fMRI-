"""Minimal NIfTI-1 header reader for pre-run parameter derivation.

Runtime geometry stays authoritative from DPABI ``y_ReadRPI`` inside MATLAB.
This reader exists for the *planning* side: it derives TR, volume count and
slice count from the actual file header instead of trusting a file name, and it
fails closed on anything it cannot parse rather than guessing a default.
"""

from __future__ import annotations

import gzip
import struct
from dataclasses import dataclass
from pathlib import Path

_HEADER_BYTES = 348
#: NIfTI-1 single-file ("n+1") and header-pair variants both start with 348.
_VALID_SIZEOF_HDR = {348, 540}
_SUPPORTED_DATATYPES = {
    2: "uint8",
    4: "int16",
    8: "int32",
    16: "float32",
    64: "float64",
    256: "int8",
    512: "uint16",
    768: "uint32",
}


class NiftiHeaderError(ValueError):
    pass


@dataclass(frozen=True)
class NiftiHeaderInfo:
    """Header facts a scientific plan needs before MATLAB is started."""

    ndim: int
    nx: int
    ny: int
    nz: int
    nt: int
    datatype: str
    pixdim_mm: tuple[float, float, float]
    tr_seconds: float
    little_endian: bool

    @property
    def voxel_count(self) -> int:
        return self.nx * self.ny * self.nz

    @property
    def volume_count(self) -> int:
        """Time points, treating a 3D image as a single volume."""

        return self.nt if self.ndim >= 4 else 1


def read_nifti_header(path: Path) -> NiftiHeaderInfo:
    """Read the fixed NIfTI-1 header, supporting plain and gzip-compressed files."""

    opener = gzip.open if path.name.lower().endswith(".gz") else open
    try:
        with opener(path, "rb") as stream:
            raw = stream.read(_HEADER_BYTES)
    except OSError as exc:
        raise NiftiHeaderError(f"cannot read NIfTI header: {path.name}") from exc
    if len(raw) < _HEADER_BYTES:
        raise NiftiHeaderError(f"NIfTI header is truncated: {path.name}")

    little_size = struct.unpack_from("<i", raw, 0)[0]
    big_size = struct.unpack_from(">i", raw, 0)[0]
    if little_size in _VALID_SIZEOF_HDR:
        endian = "<"
    elif big_size in _VALID_SIZEOF_HDR:
        endian = ">"
    else:
        raise NiftiHeaderError(f"not a NIfTI-1 file: {path.name}")

    dim = struct.unpack_from(f"{endian}8h", raw, 40)
    datatype_code = struct.unpack_from(f"{endian}h", raw, 70)[0]
    pixdim = struct.unpack_from(f"{endian}8f", raw, 76)

    ndim = dim[0]
    if ndim < 3 or ndim > 4:
        raise NiftiHeaderError(f"only 3D and 4D images are supported, got {ndim}D: {path.name}")
    nx, ny, nz = dim[1], dim[2], dim[3]
    nt = dim[4] if ndim >= 4 else 1
    if min(nx, ny, nz, nt) < 1:
        raise NiftiHeaderError(f"header declares an empty dimension: {path.name}")

    datatype = _SUPPORTED_DATATYPES.get(datatype_code)
    if datatype is None:
        raise NiftiHeaderError(f"unsupported NIfTI datatype code {datatype_code}: {path.name}")

    tr_seconds = float(pixdim[4])
    if ndim >= 4 and tr_seconds <= 0:
        raise NiftiHeaderError(
            f"4D image declares a non-positive TR ({tr_seconds}); refusing to guess: {path.name}"
        )

    return NiftiHeaderInfo(
        ndim=ndim,
        nx=nx,
        ny=ny,
        nz=nz,
        nt=nt,
        datatype=datatype,
        pixdim_mm=(float(pixdim[1]), float(pixdim[2]), float(pixdim[3])),
        tr_seconds=tr_seconds,
        little_endian=endian == "<",
    )
