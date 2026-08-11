"""ROI signal contracts and deterministic tabular exports."""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable
from pathlib import Path

from openpyxl import Workbook  # type: ignore[import-untyped]

from neuroagent.analysis.models import RoiExtractionRequest, RoiSignalRecord


def validate_roi_request(request: RoiExtractionRequest) -> tuple[str, ...]:
    issues: list[str] = []
    if request.band_low_hz >= request.band_high_hz:
        issues.append("roi_frequency_band_invalid")
    if request.band_high_hz > 1 / (2 * request.tr_seconds):
        issues.append("roi_frequency_band_exceeds_nyquist")
    if request.scrubbing_timing != "disabled" and not request.scrubbing_method:
        issues.append("roi_scrubbing_method_required")
    if request.scrubbing_timing == "disabled" and request.scrubbing_method:
        issues.append("roi_scrubbing_method_without_timing")
    if len(request.selected_roi_indices) != len(set(request.selected_roi_indices)):
        issues.append("roi_indices_duplicate")
    return tuple(issues)


def build_roi_tables(
    records: Iterable[RoiSignalRecord],
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    ordered = sorted(
        records,
        key=lambda item: (
            item.subject_id,
            item.session_id or "",
            item.metric,
            item.atlas_id,
            item.roi_index,
        ),
    )
    seen: set[tuple[str, str | None, str, str, int]] = set()
    long_rows: list[dict[str, object]] = []
    wide: dict[tuple[str, str | None, str, str], dict[str, object]] = {}
    for record in ordered:
        key = (
            record.subject_id,
            record.session_id,
            record.metric,
            record.atlas_id,
            record.roi_index,
        )
        if key in seen:
            raise ValueError(f"duplicate ROI signal record: {key}")
        seen.add(key)
        row = record.model_dump(mode="json")
        long_rows.append(row)
        wide_key = key[:4]
        wide_row = wide.setdefault(
            wide_key,
            {
                "subject_id": record.subject_id,
                "session_id": record.session_id,
                "metric": record.metric,
                "atlas_id": record.atlas_id,
            },
        )
        label = re.sub(r"[^A-Za-z0-9_]+", "_", record.roi_label).strip("_").lower()
        column = f"roi_{record.roi_index}_{label or 'unnamed'}"
        if column in wide_row:
            raise ValueError(f"ROI label collision after normalization: {record.roi_label}")
        wide_row[column] = record.value
    wide_rows = tuple(wide[key] for key in sorted(wide))
    return tuple(long_rows), wide_rows


def write_roi_exports(
    output_directory: Path,
    long_rows: tuple[dict[str, object], ...],
    wide_rows: tuple[dict[str, object], ...],
) -> tuple[Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    csv_path = output_directory / "roi_signals_long.csv"
    xlsx_path = output_directory / "roi_signals.xlsx"
    long_fields = (
        "subject_id",
        "session_id",
        "metric",
        "atlas_id",
        "roi_index",
        "roi_label",
        "value",
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=long_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(long_rows)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "roi_long"
    sheet.append(list(long_fields))
    for row in long_rows:
        sheet.append([row.get(field) for field in long_fields])
    wide_sheet = workbook.create_sheet("roi_wide")
    wide_fields = sorted({field for row in wide_rows for field in row})
    wide_sheet.append(wide_fields)
    for row in wide_rows:
        wide_sheet.append([row.get(field) for field in wide_fields])
    workbook.save(xlsx_path)
    return csv_path, xlsx_path
