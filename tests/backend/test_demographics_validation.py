from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from neuroagent.application.contracts import DemographicsImportRequest
from neuroagent.application.errors import InputValidationError
from neuroagent.infrastructure.filesystem.demographics import read_demographics


@pytest.mark.parametrize("extension,delimiter", (("csv", ","), ("tsv", "\t")))
@pytest.mark.parametrize(
    "header,row,expected_code",
    (
        (("participant", "group", "group"), ("sub-01", "A", "B"), "demographics_header_duplicate"),
        (("participant", "", "age"), ("sub-01", "A", "20"), "demographics_header_invalid"),
        (("participant", "group", "age"), ("sub-01", "A"), "demographics_row_width_invalid"),
        (
            ("participant", "group", "age"),
            ("sub-01", "A", "20", "unexpected"),
            "demographics_row_width_invalid",
        ),
        (("participant", "group", "age"), ("sub-01", "", "20"), "demographics_cell_missing"),
    ),
)
def test_csv_and_tsv_reject_ambiguous_tables(
    tmp_path: Path,
    extension: str,
    delimiter: str,
    header: tuple[str, ...],
    row: tuple[str, ...],
    expected_code: str,
) -> None:
    path = tmp_path / f"demographics.{extension}"
    path.write_text(
        f"{delimiter.join(header)}\n{delimiter.join(row)}\n",
        encoding="utf-8",
    )

    with pytest.raises(InputValidationError) as exc_info:
        read_demographics(
            path,
            subject_id_column="participant",
            column_mapping={"group": "group"},
            encoding="utf-8",
            manifest_subject_ids={"sub-01"},
        )

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    "header,row,expected_code",
    (
        (("participant", "group", "group"), ("sub-01", "A", "B"), "demographics_header_duplicate"),
        (("participant", None, "age"), ("sub-01", "A", 20), "demographics_header_invalid"),
        (("participant", "group", "age"), ("sub-01", "A"), "demographics_row_width_invalid"),
        (("participant", "group", "age"), ("sub-01", "", 20), "demographics_cell_missing"),
        (
            ("participant", "group"),
            ("sub-01", "A", "unexpected"),
            "demographics_row_width_invalid",
        ),
    ),
)
def test_xlsx_uses_the_same_header_and_missing_cell_rules(
    tmp_path: Path,
    header: tuple[object, ...],
    row: tuple[object, ...],
    expected_code: str,
) -> None:
    path = tmp_path / "demographics.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(header)
    sheet.append(row)
    workbook.save(path)
    workbook.close()

    with pytest.raises(InputValidationError) as exc_info:
        read_demographics(
            path,
            subject_id_column="participant",
            column_mapping={"group": "group"},
            encoding="utf-8",
            manifest_subject_ids={"sub-01"},
        )

    assert exc_info.value.code == expected_code


def test_valid_xlsx_remains_aligned_and_normalized(tmp_path: Path) -> None:
    path = tmp_path / "demographics.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(("participant", "group", "age"))
    sheet.append(("sub-02", "B", 30))
    sheet.append(("sub-01", "A", 20))
    workbook.save(path)
    workbook.close()

    result = read_demographics(
        path,
        subject_id_column="participant",
        column_mapping={"group": "group", "age": "age"},
        encoding="utf-8",
        manifest_subject_ids={"sub-01", "sub-02"},
    )

    assert [row["subject_id"] for row in result["rows"]] == ["sub-01", "sub-02"]
    assert result["missing_subject_ids"] == []
    assert result["extra_subject_ids"] == []


def test_reserved_subject_id_mapping_cannot_override_canonical_identity(tmp_path: Path) -> None:
    path = tmp_path / "demographics.csv"
    path.write_text("participant_id,group\nsub-01,control\n", encoding="utf-8")

    with pytest.raises(InputValidationError) as exc_info:
        read_demographics(
            path,
            subject_id_column="participant_id",
            column_mapping={"subject_id": "group"},
            encoding="utf-8",
            manifest_subject_ids={"sub-01"},
        )

    assert exc_info.value.code == "demographics_subject_id_reserved"


@pytest.mark.parametrize(
    "subject_id_column,column_mapping",
    (
        (" ", {"group": "group"}),
        ("participant_id", {" ": "group"}),
        ("participant_id", {"group": " "}),
        ("participant_id", {"subject_id": "group"}),
        ("participant_id", {"group": "source", "cohort": " source "}),
    ),
)
def test_demographics_request_rejects_ambiguous_mapping_names(
    subject_id_column: str, column_mapping: dict[str, str]
) -> None:
    with pytest.raises(ValidationError):
        DemographicsImportRequest(
            source_path="demographics.csv",
            subject_id_column=subject_id_column,
            column_mapping=column_mapping,
            expected_dataset_version=1,
        )
