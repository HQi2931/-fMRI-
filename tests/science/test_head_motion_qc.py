from __future__ import annotations

from pathlib import Path

import pytest

from neuroagent.infrastructure.filesystem.head_motion_qc import parse_dpabi_head_motion_qc

HEADER = (
    "Subject ID\tmax(abs(Tx))\tmax(abs(Ty))\tmax(abs(Tz))\t"
    "max(abs(Rx))\tmax(abs(Ry))\tmax(abs(Rz))\t"
    "mean(abs(Tx))\tmean(abs(Ty))\tmean(abs(Tz))\t"
    "mean(abs(Rx))\tmean(abs(Ry))\tmean(abs(Rz))\t"
    "mean RMS\tmean relative RMS (mean FD_VanDijk)\tmean FD_Power\t"
    "Number of FD_Power>0.5\tPercent of FD_Power>0.5\t"
    "Number of FD_Power>0.2\tPercent of FD_Power>0.2\tmean FD_Jenkinson\n"
)


def _write_subject(root: Path, subject_id: str, *, fd_values: tuple[float, ...]) -> str:
    subject_dir = root / subject_id
    subject_dir.mkdir(parents=True, exist_ok=True)
    fd_text = "\n".join(f"{value:.8f}" for value in fd_values) + "\n"
    for method in ("FD_Power", "FD_Jenkinson", "FD_VanDijk"):
        (subject_dir / f"{method}_{subject_id}.txt").write_text(fd_text, encoding="utf-8")
    (subject_dir / f"rp_{subject_id}.txt").write_text(
        "\n".join("0 0 0 0 0 0" for _ in range(len(fd_values))) + "\n",
        encoding="utf-8",
    )
    fd_gt_05 = sum(value > 0.5 for value in fd_values)
    fd_gt_02 = sum(value > 0.2 for value in fd_values)
    return (
        f"{subject_id}\t0.1\t0.1\t0.1\t0.1\t0.1\t0.1\t"
        "0.01\t0.01\t0.01\t0.01\t0.01\t0.01\t0.01\t0.05\t"
        f"{sum(fd_values) / len(fd_values):.8f}\t{fd_gt_05}\t"
        f"{fd_gt_05 / len(fd_values):.8f}\t{fd_gt_02}\t"
        f"{fd_gt_02 / len(fd_values):.8f}\t0.05\n"
    )


def _write_exclusions(root: Path, subject_ids: tuple[str, ...] = ()) -> None:
    root.joinpath("ExcludeSubjectsAccordingToMaxHeadMotion.txt").write_text(
        "Excluding Criteria: 2.0mm and 2.0 degree in max head motion\n"
        + ("\n".join(subject_ids) if subject_ids else "None")
        + "\n",
        encoding="utf-8",
    )


def test_parse_dpabi_head_motion_qc_partitions_subjects(tmp_path: Path) -> None:
    root = tmp_path / "RealignParameter"
    root.mkdir()
    rows = [
        HEADER,
        _write_subject(root, "sub-01", fd_values=(0.0, 0.05, 0.06, 0.04)),
        _write_subject(root, "sub-02", fd_values=(0.0, 0.7, 0.8, 0.6)),
    ]
    root.joinpath("HeadMotion.tsv").write_text("".join(rows), encoding="utf-8")
    _write_exclusions(root, ("sub-02",))

    report = parse_dpabi_head_motion_qc(
        root,
        input_manifest_hash="a" * 64,
        subject_ids=("sub-01", "sub-02"),
        expected_volume_counts={"sub-01": 4, "sub-02": 4},
    )

    assert report.included_subject_ids == ("sub-01",)
    assert report.excluded_subject_ids == ("sub-02",)
    assert report.subjects[1].dpabi_excluded_at_2mm_2deg
    assert "FD Power >0.5" in report.exclusion_reasons[0][1]


def test_parse_dpabi_head_motion_qc_fails_closed_on_nonfinite_values(
    tmp_path: Path,
) -> None:
    root = tmp_path / "RealignParameter"
    root.mkdir()
    root.joinpath("HeadMotion.tsv").write_text(
        HEADER + "sub-01\t0.1\t0.1\t0.1\tnan\t0.1\t0.1\t"
        "0.01\t0.01\t0.01\t0.01\t0.01\t0.01\t0.01\t0.05\t"
        "0.05\t0\t0\t0\t0\t0.05\n",
        encoding="utf-8",
    )
    subject_dir = root / "sub-01"
    subject_dir.mkdir()
    for filename in (
        "FD_Power_sub-01.txt",
        "FD_Jenkinson_sub-01.txt",
        "FD_VanDijk_sub-01.txt",
        "rp_sub-01.txt",
    ):
        subject_dir.joinpath(filename).write_text("0 0 0 0 0 0\n", encoding="utf-8")
    _write_exclusions(root)

    with pytest.raises(ValueError, match="non-finite head-motion value"):
        parse_dpabi_head_motion_qc(
            root,
            input_manifest_hash="a" * 64,
            subject_ids=("sub-01",),
            expected_volume_counts={"sub-01": 1},
        )
