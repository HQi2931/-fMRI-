from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from urllib.error import URLError

import pytest

from scripts import rsfmri_demo_download as demo


def _participant_bytes(*, wrong_group: str | None = None) -> bytes:
    output = io.StringIO(newline="")
    fields = ["participant_id", "group", "ICD-10", "age"]
    writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for participant, group in demo.SUBJECT_GROUPS.items():
        row_group = wrong_group if wrong_group and participant == demo.DEPR_SUBJECTS[0] else group
        diagnosis = "F32.1" if group == "depr" else "n/a"
        writer.writerow(
            {
                "participant_id": participant,
                "group": row_group,
                "ICD-10": diagnosis,
                "age": "50",
            }
        )
    writer.writerow({"participant_id": "sub-01", "group": "depr", "ICD-10": "F32.0", "age": "39"})
    return output.getvalue().encode("utf-8")


def _sources(participants: bytes) -> tuple[list[demo.SourceFile], dict[str, bytes]]:
    sources: list[demo.SourceFile] = []
    payloads: dict[str, bytes] = {}
    for filename in demo.expected_remote_paths():
        if filename == "participants.tsv":
            payload = participants
        elif filename == "dataset_description.json":
            payload = json.dumps(
                {
                    "Name": "Resting state with depression and controls",
                    "License": "CC0",
                    "BIDSVersion": "1.0.2",
                    "DatasetDOI": "10.18112/openneuro.ds002748.v1.0.5",
                    "HowToAcknowledge": "Cite the source study.",
                }
            ).encode()
        elif filename == "task-rest_bold.json":
            payload = json.dumps({"RepetitionTime": 2.5}).encode()
        else:
            payload = f"OpenNeuro source: {filename}".encode()
        payloads[filename] = payload
        sources.append(
            demo.SourceFile(
                filename=filename,
                size=len(payload),
                url=f"https://s3.amazonaws.com/openneuro.org/{demo.DATASET_ID}/{filename}?versionId=pinned",
            )
        )
    return sources, payloads


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


def _url_filename(request: object) -> str:
    url = getattr(request, "full_url", str(request))
    return url.split(f"/{demo.DATASET_ID}/", maxsplit=1)[1].split("?", maxsplit=1)[0]


def test_fixed_selection_is_balanced_and_excludes_non_target_diagnosis() -> None:
    output, rows = demo.select_participants(_participant_bytes())
    parsed = list(csv.DictReader(io.StringIO(output.decode()), delimiter="\t"))

    assert len(rows) == len(parsed) == 20
    assert [row["participant_id"] for row in parsed] == list(demo.SUBJECT_GROUPS)
    assert {row["group"] for row in parsed} == {"depr", "control"}
    assert sum(row["group"] == "depr" for row in parsed) == 10
    assert sum(row["group"] == "control" for row in parsed) == 10
    assert all(row["ICD-10"] in {"F32.0", "F32.1"} for row in parsed if row["group"] == "depr")
    assert "sub-01" not in output.decode()


@pytest.mark.parametrize("wrong_group", ["control", "unknown"])
def test_selection_fails_closed_on_group_mismatch(wrong_group: str) -> None:
    with pytest.raises(demo.DownloadError, match="wrong group label"):
        demo.select_participants(_participant_bytes(wrong_group=wrong_group))


def test_snapshot_subset_contains_required_raw_bids_files() -> None:
    sources, _ = _sources(_participant_bytes())
    selected = demo.select_source_files(sources)

    assert len(selected) == 46
    assert {item.filename for item in selected} == set(demo.expected_remote_paths())
    assert all(
        f"{subject}/anat/{subject}_T1w.nii.gz" in {item.filename for item in selected}
        and f"{subject}/func/{subject}_task-rest_bold.nii.gz"
        in {item.filename for item in selected}
        for subject in demo.SUBJECT_GROUPS
    )


def test_openneuro_file_urls_must_be_snapshot_bound_and_first_party() -> None:
    demo._safe_source_url(
        "sub-21/func/sub-21_task-rest_bold.nii.gz",
        "https://s3.amazonaws.com/openneuro.org/ds002748/sub-21/func/sub-21_task-rest_bold.nii.gz?versionId=abc",
    )
    demo._safe_source_url(
        "README",
        "https://openneuro.org/crn/datasets/ds002748/objects/"
        "fe9fd83346040cb29430e0fa009f0a1cd8a22550?filename=README",
    )
    with pytest.raises(demo.DownloadError, match="Unexpected OpenNeuro file URL"):
        demo._safe_source_url("README", "https://untrusted.example/file?filename=README")


def test_pinned_dataset_metadata_confirms_license_doi_and_rest_tr(tmp_path: Path) -> None:
    (tmp_path / "dataset_description.json").write_text(
        json.dumps(
            {
                "Name": "Resting state demo",
                "License": "CC0",
                "BIDSVersion": "1.0.2",
                "DatasetDOI": "10.18112/openneuro.ds002748.v1.0.5",
                "HowToAcknowledge": "Cite the source study.",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "task-rest_bold.json").write_text(
        json.dumps({"RepetitionTime": 2.5}), encoding="utf-8"
    )

    details = demo._validated_dataset_details(tmp_path)

    assert details["license"] == "CC0"
    assert details["dataset_doi"] == "10.18112/openneuro.ds002748.v1.0.5"
    assert details["repetition_time_seconds"] == 2.5
    (tmp_path / "task-rest_bold.json").write_text(
        json.dumps({"RepetitionTime": 2.0}), encoding="utf-8"
    )
    with pytest.raises(demo.DownloadError, match=r"TR = 2\.5"):
        demo._validated_dataset_details(tmp_path)


def test_download_is_atomic_idempotent_and_records_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    participants = _participant_bytes()
    sources, payloads = _sources(participants)
    fail_once = {"README": True}

    def fake_urlopen(request: object, timeout: int) -> _Response:
        filename = _url_filename(request)
        if filename == "README" and fail_once.pop("README", False):
            raise URLError("temporary network interruption")
        return _Response(payloads[filename])

    monkeypatch.setattr(demo, "urlopen", fake_urlopen)
    destination = tmp_path / "ds002748"

    def load_files(_timeout: int) -> list[demo.SourceFile]:
        return sources

    with pytest.raises(demo.DownloadError, match="Failed to download README"):
        demo.download_demo(destination, files_loader=load_files)
    assert not destination.exists()
    assert not list(tmp_path.glob(".rsfmri-demo-*"))

    downloaded = demo.download_demo(destination, files_loader=load_files)
    assert downloaded["status"] == "downloaded"

    with (destination / "participants.tsv").open(encoding="utf-8", newline="") as stream:
        participant_rows = list(csv.DictReader(stream, delimiter="\t"))
    assert len(participant_rows) == 20
    assert (destination / "sub-21/func/sub-21_task-rest_bold.nii.gz").read_bytes() == payloads[
        "sub-21/func/sub-21_task-rest_bold.nii.gz"
    ]
    manifest = json.loads((destination / "demo-manifest.json").read_text(encoding="utf-8"))
    assert manifest["groups"] == {"depr": 10, "control": 10}
    assert len(manifest["output_files"]) == 47
    assert all(len(item["sha256"]) == 64 for item in manifest["output_files"])

    second = demo.download_demo(destination, files_loader=load_files)
    assert second["status"] == "already_complete"
    assert second["output_files"] == manifest["output_files"]


def test_existing_unverified_output_is_never_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "already-here"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("user data", encoding="utf-8")
    sources, _ = _sources(_participant_bytes())

    with pytest.raises(demo.DownloadError, match="without a verified manifest"):
        demo.download_demo(destination, files_loader=lambda _timeout: sources)

    assert sentinel.read_text(encoding="utf-8") == "user data"


def test_snapshot_selector_rejects_missing_file_and_unsafe_path() -> None:
    sources, _ = _sources(_participant_bytes())
    with pytest.raises(demo.DownloadError, match="missing required files"):
        demo.select_source_files(sources[:-1])
    with pytest.raises(demo.DownloadError, match="Unsafe source path"):
        demo._safe_relative_path("../../outside.nii.gz")
