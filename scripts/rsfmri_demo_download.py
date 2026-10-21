"""Download a pinned, balanced rs-fMRI subset from OpenNeuro for local demos."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

DATASET_ID = "ds002748"
SNAPSHOT = "1.0.5"
DATASET_URL = f"https://openneuro.org/datasets/{DATASET_ID}/versions/{SNAPSHOT}"
GRAPHQL_URL = "https://openneuro.org/crn/graphql"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data/rsfmri-demo/ds002748-v1.0.5"
DOWNLOAD_ATTEMPTS = 4

DEPR_SUBJECTS = (*(f"sub-{number:02d}" for number in range(21, 30)), "sub-31")
CONTROL_SUBJECTS = (
    "sub-52",
    "sub-53",
    "sub-55",
    "sub-56",
    "sub-57",
    "sub-58",
    "sub-60",
    "sub-62",
    "sub-68",
    "sub-69",
)
SUBJECT_GROUPS = {
    **{subject: "depr" for subject in DEPR_SUBJECTS},
    **{subject: "control" for subject in CONTROL_SUBJECTS},
}
ROOT_FILES = (
    "CHANGES",
    "README",
    "dataset_description.json",
    "participants.json",
    "participants.tsv",
    "task-rest_bold.json",
)
GRAPHQL_QUERY = f"""
query RsFmriDemoSnapshot {{
  snapshot(datasetId: "{DATASET_ID}", tag: "{SNAPSHOT}") {{
    files(recursive: true) {{ filename size urls }}
  }}
}}
"""


@dataclass(frozen=True)
class SourceFile:
    filename: str
    size: int
    url: str


class DownloadError(RuntimeError):
    """Raised when source data or a local destination fails validation."""


def _safe_relative_path(filename: str) -> Path:
    relative = PurePosixPath(filename)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in filename
    ):
        raise DownloadError(f"Unsafe source path in dataset file listing: {filename!r}")
    return Path(*relative.parts)


def _safe_source_url(filename: str, url: str) -> None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if parsed.scheme != "https":
        raise DownloadError(f"Unexpected OpenNeuro file URL for {filename!r}")
    if parsed.hostname == "s3.amazonaws.com":
        if parsed.path != f"/openneuro.org/{DATASET_ID}/{filename}" or not query.get("versionId"):
            raise DownloadError(f"OpenNeuro S3 URL for {filename!r} is not snapshot-pinned")
        return
    object_match = re.fullmatch(rf"/crn/datasets/{DATASET_ID}/objects/[0-9a-f]{{40}}", parsed.path)
    if (
        parsed.hostname != "openneuro.org"
        or object_match is None
        or query.get("filename") != [filename]
    ):
        raise DownloadError(f"Unexpected OpenNeuro file URL for {filename!r}")


def _request_json(url: str, payload: bytes, timeout: int) -> dict[str, Any]:
    request = Request(
        url,
        data=payload,
        headers={
            "User-Agent": "rs-fmri-agent-demo-downloader/1.0",
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise DownloadError(f"Could not read OpenNeuro metadata: {exc}") from exc
    if not isinstance(result, dict):
        raise DownloadError("OpenNeuro returned invalid metadata")
    return result


def fetch_snapshot_files(timeout: int = 60) -> list[SourceFile]:
    response = _request_json(
        GRAPHQL_URL,
        json.dumps({"query": GRAPHQL_QUERY}).encode(),
        timeout,
    )
    if response.get("errors"):
        raise DownloadError(f"OpenNeuro GraphQL query failed: {response['errors']}")
    try:
        raw_files = response["data"]["snapshot"]["files"]
    except (KeyError, TypeError) as exc:
        raise DownloadError("OpenNeuro response is missing the pinned snapshot file list") from exc
    if not isinstance(raw_files, list):
        raise DownloadError("OpenNeuro response contains an invalid snapshot file list")

    files: list[SourceFile] = []
    for item in raw_files:
        if not isinstance(item, dict):
            raise DownloadError("OpenNeuro returned a malformed file entry")
        filename, size, urls = item.get("filename"), item.get("size"), item.get("urls")
        if not isinstance(filename, str) or not isinstance(size, int) or size < 0:
            raise DownloadError("OpenNeuro returned a file without a valid name or size")
        if not isinstance(urls, list) or not urls or not isinstance(urls[0], str):
            raise DownloadError(f"OpenNeuro did not provide a download URL for {filename!r}")
        files.append(SourceFile(filename=filename, size=size, url=urls[0]))
    return files


def expected_remote_paths() -> tuple[str, ...]:
    paths = list(ROOT_FILES)
    for subject in (*DEPR_SUBJECTS, *CONTROL_SUBJECTS):
        paths.extend(
            (
                f"{subject}/anat/{subject}_T1w.nii.gz",
                f"{subject}/func/{subject}_task-rest_bold.nii.gz",
            )
        )
    return tuple(paths)


def select_source_files(files: list[SourceFile]) -> list[SourceFile]:
    selected: dict[str, SourceFile] = {}
    for source in files:
        if source.filename in selected:
            raise DownloadError(f"Duplicate file in OpenNeuro snapshot: {source.filename}")
        selected[source.filename] = source
    expected = expected_remote_paths()
    missing = sorted(set(expected) - set(selected))
    if missing:
        raise DownloadError(
            f"Pinned OpenNeuro snapshot is missing required files: {', '.join(missing)}"
        )
    result = [selected[name] for name in expected]
    for source in result:
        _safe_relative_path(source.filename)
        _safe_source_url(source.filename, source.url)
    return result


def select_participants(payload: bytes) -> tuple[bytes, list[dict[str, str]]]:
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")), delimiter="\t")
    except UnicodeDecodeError as exc:
        raise DownloadError("OpenNeuro participants.tsv is not UTF-8 text") from exc
    fields = reader.fieldnames
    if not fields or not {"participant_id", "group", "ICD-10"}.issubset(fields):
        raise DownloadError(
            "participants.tsv lacks a required participant, group, or diagnosis column"
        )

    by_id: dict[str, dict[str, str]] = {}
    for csv_row in reader:
        subject = csv_row.get("participant_id", "")
        if not subject or subject in by_id:
            raise DownloadError(f"participants.tsv has a missing or duplicate ID: {subject!r}")
        by_id[subject] = {key: value or "n/a" for key, value in csv_row.items() if key is not None}

    selected: list[dict[str, str]] = []
    for subject, expected_group in SUBJECT_GROUPS.items():
        participant = by_id.get(subject)
        if participant is None:
            raise DownloadError(f"Selected participant is missing from participants.tsv: {subject}")
        if participant.get("group") != expected_group:
            raise DownloadError(f"Selected participant {subject} has the wrong group label")
        if expected_group == "depr" and participant.get("ICD-10") not in {"F32.0", "F32.1"}:
            raise DownloadError(
                f"Selected depression participant {subject} has an unexpected diagnosis"
            )
        selected.append(participant)

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(selected)
    return output.getvalue().encode("utf-8"), selected


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download_bytes(source: SourceFile, timeout: int = 90) -> bytes:
    request = Request(source.url, headers={"User-Agent": "rs-fmri-agent-demo-downloader/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = cast(bytes, response.read())
    except (OSError, URLError) as exc:
        raise DownloadError(f"Failed to download {source.filename}: {exc}") from exc
    if len(payload) != source.size:
        raise DownloadError(
            f"Size mismatch for {source.filename}: OpenNeuro lists {source.size} bytes, "
            f"got {len(payload)}"
        )
    return payload


def _download_file(source: SourceFile, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.part")
    request = Request(source.url, headers={"User-Agent": "rs-fmri-agent-demo-downloader/1.0"})
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        digest = hashlib.sha256()
        size = 0
        try:
            with urlopen(request, timeout=90) as response, temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                    output.write(chunk)
            if size != source.size:
                raise DownloadError(
                    f"Size mismatch for {source.filename}: OpenNeuro lists {source.size} bytes, "
                    f"got {size}"
                )
            os.replace(temporary, destination)
            return {"filename": source.filename, "size": size, "sha256": digest.hexdigest()}
        except (OSError, URLError, DownloadError) as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            if attempt == DOWNLOAD_ATTEMPTS:
                break
            time.sleep(min(2 ** (attempt - 1), 4))

    raise DownloadError(
        f"Failed to download {source.filename} after {DOWNLOAD_ATTEMPTS} attempts: {last_error}"
    ) from last_error


def _output_files(root: Path) -> list[dict[str, Any]]:
    paths = list(root.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise DownloadError(f"Symlinks are not allowed in demo data: {root}")
    records: list[dict[str, Any]] = []
    for path in sorted(
        (item for item in paths if item.is_file()),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        if relative != "demo-manifest.json":
            records.append(
                {"filename": relative, "size": path.stat().st_size, "sha256": _sha256(path)}
            )
    return records


def _validated_dataset_details(root: Path) -> dict[str, Any]:
    try:
        description = json.loads((root / "dataset_description.json").read_text(encoding="utf-8"))
        task = json.loads((root / "task-rest_bold.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DownloadError("OpenNeuro dataset metadata is unreadable") from exc
    if not isinstance(description, dict) or description.get("License") != "CC0":
        raise DownloadError("The pinned dataset no longer declares the expected CC0 license")
    if description.get("DatasetDOI") != "10.18112/openneuro.ds002748.v1.0.5":
        raise DownloadError(
            "dataset_description.json does not identify the pinned OpenNeuro snapshot"
        )
    if not isinstance(task, dict) or task.get("RepetitionTime") != 2.5:
        raise DownloadError("The pinned resting-state sidecar does not declare TR = 2.5 seconds")
    return {
        "name": description.get("Name"),
        "license": description["License"],
        "dataset_doi": description["DatasetDOI"],
        "bids_version": description.get("BIDSVersion"),
        "citation": description.get("HowToAcknowledge"),
        "repetition_time_seconds": task["RepetitionTime"],
    }


def _existing_download(destination: Path) -> dict[str, Any] | None:
    if not destination.exists():
        return None
    if not destination.is_dir() or destination.is_symlink():
        raise DownloadError(f"Output path exists and is not a normal directory: {destination}")
    manifest_path = destination / "demo-manifest.json"
    if not manifest_path.is_file():
        raise DownloadError(
            f"Output directory already exists without a verified manifest: {destination}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DownloadError(f"Existing demo manifest is invalid: {manifest_path}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("dataset_id") != DATASET_ID
        or manifest.get("snapshot") != SNAPSHOT
        or manifest.get("groups") != {"depr": 10, "control": 10}
    ):
        raise DownloadError(
            f"Output directory belongs to a different or incomplete demo: {destination}"
        )
    expected = manifest.get("output_files")
    if not isinstance(expected, list) or _output_files(destination) != expected:
        raise DownloadError(f"Existing demo output has changed since download: {destination}")
    return manifest


def download_demo(
    destination: Path = DEFAULT_OUTPUT,
    *,
    dry_run: bool = False,
    timeout: int = 60,
    files_loader: Callable[[int], list[SourceFile]] = fetch_snapshot_files,
    metadata_downloader: Callable[[SourceFile, int], bytes] | None = None,
    file_downloader: Callable[[SourceFile, Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    sources = select_source_files(files_loader(timeout))
    destination = destination.expanduser().absolute()
    prior = _existing_download(destination)
    if prior is not None:
        return {"status": "already_complete", **prior}

    plan = {
        "dataset_id": DATASET_ID,
        "snapshot": SNAPSHOT,
        "source_url": DATASET_URL,
        "selected_subjects": SUBJECT_GROUPS,
        "source_file_count": len(sources),
        "download_bytes": sum(item.size for item in sources),
        "output": str(destination),
    }
    if dry_run:
        return {"status": "dry_run", **plan}

    metadata_file = next(item for item in sources if item.filename == "participants.tsv")
    get_metadata = metadata_downloader or _download_bytes
    participant_source = get_metadata(metadata_file, timeout)
    if len(participant_source) != metadata_file.size:
        raise DownloadError(
            f"Size mismatch for participants.tsv: OpenNeuro lists {metadata_file.size} bytes, "
            f"got {len(participant_source)}"
        )
    subset_table, selected_rows = select_participants(participant_source)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".rsfmri-demo-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "dataset"
        stage.mkdir()
        get_file = file_downloader or _download_file
        source_files: list[dict[str, Any]] = []
        local_files: list[dict[str, Any]] = []
        for source in sources:
            if source.filename == "participants.tsv":
                source_files.append(
                    {
                        "filename": source.filename,
                        "size": source.size,
                        "sha256": hashlib.sha256(participant_source).hexdigest(),
                        "url": source.url,
                    }
                )
                participants_path = stage / "participants.tsv"
                participants_path.write_bytes(subset_table)
                local_files.append(
                    {
                        "filename": "participants.tsv",
                        "size": participants_path.stat().st_size,
                        "sha256": _sha256(participants_path),
                    }
                )
                continue
            relative = _safe_relative_path(source.filename)
            record = get_file(source, stage / relative)
            source_files.append({**record, "url": source.url})
            local_files.append(record)

        dataset_details = _validated_dataset_details(stage)
        provenance = {
            "dataset_id": DATASET_ID,
            "snapshot": SNAPSHOT,
            "source_url": DATASET_URL,
            **dataset_details,
            "source_participants_sha256": hashlib.sha256(participant_source).hexdigest(),
            "selection": {"depr": list(DEPR_SUBJECTS), "control": list(CONTROL_SUBJECTS)},
            "selected_subject_count": len(selected_rows),
            "source_files": source_files,
        }
        provenance_path = stage / "demo-provenance.json"
        provenance_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        local_files.append(
            {
                "filename": "demo-provenance.json",
                "size": provenance_path.stat().st_size,
                "sha256": _sha256(provenance_path),
            }
        )
        manifest = {
            "dataset_id": DATASET_ID,
            "snapshot": SNAPSHOT,
            "source_url": DATASET_URL,
            "selected_subject_count": len(selected_rows),
            "groups": {
                group: sum(row["group"] == group for row in selected_rows)
                for group in ("depr", "control")
            },
            "output_files": sorted(local_files, key=lambda item: item["filename"]),
        }
        (stage / "demo-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if destination.exists():
            raise DownloadError(f"Output directory appeared during download: {destination}")
        stage.replace(destination)

    verified = _existing_download(destination)
    if verified is None:
        raise DownloadError(f"Downloaded demo directory is missing: {destination}")
    return {"status": "downloaded", **verified, "output": str(destination)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="New output directory (default: data/rsfmri-demo/ds002748-v1.0.5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the pinned files and estimated size without writing data",
    )
    parser.add_argument("--timeout", type=int, default=60, help="Network timeout in seconds")
    args = parser.parse_args(argv)
    try:
        result = download_demo(args.output, dry_run=args.dry_run, timeout=args.timeout)
    except DownloadError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
