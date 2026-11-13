"""Parse DPABI RealignParameter outputs into typed head-motion QC evidence."""

from __future__ import annotations

import csv
import math
import re
from pathlib import Path

from neuroagent.domain.fmri.qc import (
    HeadMotionQcCriteria,
    HeadMotionQcReport,
    SubjectHeadMotionQcResult,
)

PARSER_VERSION = "dpabi-head-motion-qc-v1"


def parse_dpabi_head_motion_qc(
    realign_parameter_dir: Path,
    *,
    input_manifest_hash: str,
    subject_ids: tuple[str, ...],
    criteria: HeadMotionQcCriteria | None = None,
    expected_volume_counts: dict[str, int] | None = None,
) -> HeadMotionQcReport:
    criteria = criteria or HeadMotionQcCriteria()
    root = realign_parameter_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("RealignParameter path is not a directory")
    rows = _read_head_motion_tsv(root / "HeadMotion.tsv")
    if set(rows) != set(subject_ids):
        raise ValueError("HeadMotion.tsv subjects do not match the frozen manifest")
    excluded_at_2 = _read_dpabi_exclusion_file(
        root / "ExcludeSubjectsAccordingToMaxHeadMotion.txt"
    ).get((2.0, 2.0), set())
    results: list[SubjectHeadMotionQcResult] = []
    included: list[str] = []
    excluded: list[str] = []
    reasons: list[tuple[str, str]] = []
    for subject_id in subject_ids:
        row = rows[subject_id]
        subject_dir = root / subject_id
        fd_power = _read_vector(_single_file(subject_dir, f"FD_Power_{subject_id}.txt"))
        fd_jenkinson = _read_vector(_single_file(subject_dir, f"FD_Jenkinson_{subject_id}.txt"))
        fd_vandijk = _read_vector(_single_file(subject_dir, f"FD_VanDijk_{subject_id}.txt"))
        rp_path = _single_glob(subject_dir, "rp_*.txt")
        rp_count = _read_rp_count(rp_path)
        expected_count = (expected_volume_counts or {}).get(subject_id)
        if expected_count is not None:
            _validate_motion_lengths(subject_id, expected_count, fd_power, rp_count)
        if len(fd_jenkinson) != len(fd_power) or len(fd_vandijk) != len(fd_power):
            raise ValueError(f"FD method sample counts disagree for {subject_id}")
        max_translation = max(row["max(abs(Tx))"], row["max(abs(Ty))"], row["max(abs(Tz))"])
        max_rotation = max(row["max(abs(Rx))"], row["max(abs(Ry))"], row["max(abs(Rz))"])
        mean_fd_power = row["mean FD_Power"]
        fd_gt_0_5_count = round(row["Number of FD_Power>0.5"])
        fd_gt_0_5_fraction = row["Percent of FD_Power>0.5"]
        fd_gt_0_2_count = round(row["Number of FD_Power>0.2"])
        fd_gt_0_2_fraction = row["Percent of FD_Power>0.2"]
        if fd_gt_0_5_count != sum(value > 0.5 for value in fd_power):
            raise ValueError(f"FD_Power>0.5 count disagrees for {subject_id}")
        failed = []
        if max_translation > criteria.max_translation_mm:
            failed.append(f"max translation {max_translation:.4g}mm exceeds threshold")
        if max_rotation > criteria.max_rotation_deg:
            failed.append(f"max rotation {max_rotation:.4g}deg exceeds threshold")
        if mean_fd_power > criteria.mean_fd_power:
            failed.append(f"mean FD Power {mean_fd_power:.4g} exceeds threshold")
        if fd_gt_0_5_fraction > criteria.fd_power_gt_0_5_fraction:
            failed.append(f"FD Power >0.5 fraction {fd_gt_0_5_fraction:.4g} exceeds threshold")
        passed = not failed
        if passed:
            included.append(subject_id)
        else:
            excluded.append(subject_id)
            reasons.append((subject_id, "; ".join(failed)))
        results.append(
            SubjectHeadMotionQcResult(
                subject_id=subject_id,
                passed=passed,
                max_translation_mm=max_translation,
                max_rotation_deg=max_rotation,
                mean_fd_power=mean_fd_power,
                fd_power_gt_0_5_count=fd_gt_0_5_count,
                fd_power_gt_0_5_fraction=fd_gt_0_5_fraction,
                fd_power_gt_0_2_count=fd_gt_0_2_count,
                fd_power_gt_0_2_fraction=fd_gt_0_2_fraction,
                fd_power_sample_count=len(fd_power),
                realignment_parameter_count=rp_count,
                dpabi_excluded_at_2mm_2deg=subject_id in excluded_at_2,
                failed_reasons=tuple(failed),
                evidence_relative_paths=(
                    "HeadMotion.tsv",
                    "ExcludeSubjectsAccordingToMaxHeadMotion.txt",
                    f"{subject_id}/FD_Power_{subject_id}.txt",
                    f"{subject_id}/FD_Jenkinson_{subject_id}.txt",
                    f"{subject_id}/FD_VanDijk_{subject_id}.txt",
                    f"{subject_id}/{rp_path.name}",
                ),
            )
        )
    return HeadMotionQcReport(
        input_manifest_hash=input_manifest_hash,
        criteria=criteria,
        subjects=tuple(results),
        included_subject_ids=tuple(included),
        excluded_subject_ids=tuple(excluded),
        exclusion_reasons=tuple(reasons),
        source_directory="RealignParameter",
        parser_version=PARSER_VERSION,
    )


def _read_head_motion_tsv(path: Path) -> dict[str, dict[str, float]]:
    if not path.is_file():
        raise ValueError("HeadMotion.tsv is missing")
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if reader.fieldnames is None or "Subject ID" not in reader.fieldnames:
            raise ValueError("HeadMotion.tsv header is invalid")
        rows: dict[str, dict[str, float]] = {}
        for item in reader:
            subject_id = str(item.get("Subject ID", "")).strip()
            if not subject_id:
                raise ValueError("HeadMotion.tsv contains an empty subject ID")
            if subject_id in rows:
                raise ValueError(f"HeadMotion.tsv contains duplicate subject {subject_id}")
            rows[subject_id] = {
                key: _parse_float(item.get(key), f"HeadMotion.tsv:{subject_id}:{key}")
                for key in (
                    "max(abs(Tx))",
                    "max(abs(Ty))",
                    "max(abs(Tz))",
                    "max(abs(Rx))",
                    "max(abs(Ry))",
                    "max(abs(Rz))",
                    "mean FD_Power",
                    "Number of FD_Power>0.5",
                    "Percent of FD_Power>0.5",
                    "Number of FD_Power>0.2",
                    "Percent of FD_Power>0.2",
                )
            }
    return rows


def _read_dpabi_exclusion_file(path: Path) -> dict[tuple[float, float], set[str]]:
    if not path.is_file():
        raise ValueError("DPABI max-head-motion exclusion file is missing")
    result: dict[tuple[float, float], set[str]] = {}
    current: tuple[float, float] | None = None
    pattern = re.compile(
        r"Excluding Criteria:\s*([0-9.]+)mm and ([0-9.]+) degree in max head motion"
    )
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = pattern.fullmatch(line)
        if match:
            current = (float(match.group(1)), float(match.group(2)))
            result.setdefault(current, set())
            continue
        if current is None:
            continue
        if line.casefold() == "none":
            continue
        result[current].add(line)
    return result


def _single_file(parent: Path, filename: str) -> Path:
    path = parent / filename
    if not path.is_file():
        raise ValueError(f"required head-motion evidence is missing: {filename}")
    return path


def _single_glob(parent: Path, pattern: str) -> Path:
    matches = sorted(parent.glob(pattern))
    if len(matches) != 1 or not matches[0].is_file():
        raise ValueError(f"expected exactly one {pattern} file in {parent.name}")
    return matches[0]


def _read_vector(path: Path) -> tuple[float, ...]:
    values = tuple(
        _parse_float(token, str(path)) for token in path.read_text(encoding="utf-8-sig").split()
    )
    if not values:
        raise ValueError(f"head-motion vector is empty: {path.name}")
    return values


def _read_rp_count(path: Path) -> int:
    count = 0
    for index, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        values = [_parse_float(token, f"{path.name}:{index}") for token in line.split()]
        if len(values) != 6:
            raise ValueError(f"realignment parameter row must contain six values: {path.name}")
        count += 1
    if count == 0:
        raise ValueError(f"realignment parameter file is empty: {path.name}")
    return count


def _validate_motion_lengths(
    subject_id: str, expected_count: int, fd_power: tuple[float, ...], rp_count: int
) -> None:
    if rp_count != expected_count:
        raise ValueError(f"realignment parameter count does not match volumes for {subject_id}")
    if len(fd_power) not in {expected_count, expected_count - 1}:
        raise ValueError(f"FD Power length does not match volumes for {subject_id}")


def _parse_float(value: object, label: str) -> float:
    try:
        parsed = float(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"invalid numeric head-motion value at {label}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite head-motion value at {label}")
    return parsed
