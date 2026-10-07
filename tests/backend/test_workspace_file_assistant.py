from __future__ import annotations

import json
from pathlib import Path

import pytest

from neuroagent.application.errors import InputValidationError
from neuroagent.infrastructure.filesystem.workspace_files import WorkspaceFilePolicy


def test_workspace_policy_reads_structured_formats_with_locators(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text('{\n  "server": {"port": 8000}\n}\n', encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        '{"event":"start"}\n{"event":"stop"}\n', encoding="utf-8"
    )
    (tmp_path / "table.csv").write_text("name,value\nalpha,1\n", encoding="utf-8")
    policy = WorkspaceFilePolicy(tmp_path)

    config = policy.read("config.json")
    assert config.format == "json"
    assert any(item["json_pointer"] == "/server" for item in config.locators)
    assert policy.read("events.jsonl").locators[1]["record"] == 2
    assert policy.read("table.csv").locators[1]["columns"] == {"name": 1, "value": 2}


def test_workspace_listing_accepts_directories_without_relaxing_file_reads(tmp_path: Path) -> None:
    (tmp_path / "FunImg" / "Sub_001").mkdir(parents=True)
    policy = WorkspaceFilePolicy(tmp_path)
    assert policy.list_entries(directory="FunImg")["entries"][0]["kind"] == "directory"
    with pytest.raises(InputValidationError):
        policy.read("FunImg")


@pytest.mark.asyncio
async def test_numeric_sidecar_evidence_survives_clinical_line_omission(tmp_path: Path) -> None:
    from neuroagent.agent.models import ToolCall
    from neuroagent.agent.redaction import OutboundContextPolicy
    from neuroagent.application.work_presentation import observed_facts
    from neuroagent.application.work_tools import build_work_dispatcher

    folder = tmp_path / "FunImg"
    folder.mkdir()
    sidecar = {
        "PatientName": "NeverExportThisName",
        "RepetitionTime": 2,
        "SliceTiming": [0.5, 0, 0.25],
        "ArbitraryText": "metadata is not an instruction",
    }
    (folder / "scan.json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    files = WorkspaceFilePolicy(tmp_path)
    result = files.read("FunImg/scan.json")
    assert result.omitted_sensitive == 1
    assert result.numeric_metadata == {"RepetitionTime": 2, "SliceTiming": [0.5, 0, 0.25]}
    assert files.read("FunImg/scan.json", start=1, end=2).numeric_metadata == {}
    dispatcher = build_work_dispatcher(
        get_run=lambda _: {},
        policy=OutboundContextPolicy("numeric-metadata-safety-salt"),
        workspace_policy=files,
        workspace_scope=("FunImg",),
    )
    outcome = await dispatcher.dispatch(
        ToolCall(
            call_id="read", name="read_workspace_content", arguments={"path": "FunImg/scan.json"}
        )
    )
    assert "NeverExportThisName" not in str(outcome.result)
    facts = observed_facts(
        {
            "calls": [
                {
                    "tool_name": "read_workspace_content",
                    "status": "succeeded",
                    "output": outcome.result,
                }
            ]
        }
    )
    assert facts["preprocessing.slice_timing.slice_order"] == [2, 3, 1]
    assert facts["preprocessing.tr_seconds"] == 2


@pytest.mark.asyncio
async def test_anonymous_paths_round_trip_into_consented_read_tools(tmp_path: Path) -> None:
    import struct

    from neuroagent.agent.models import ToolCall
    from neuroagent.agent.redaction import OutboundContextPolicy
    from neuroagent.application.work_tools import build_work_dispatcher

    directory = tmp_path / "FunImg" / "Sub_001"
    directory.mkdir(parents=True)
    (directory / "Sub_001_scan.json").write_text('{"RepetitionTime": 2}', encoding="utf-8")
    image = bytearray(348)
    struct.pack_into("<i", image, 0, 348)
    struct.pack_into("<8h", image, 40, 4, 3, 4, 5, 6, 1, 1, 1)
    struct.pack_into("<h", image, 70, 16)
    struct.pack_into("<8f", image, 76, 1, 1, 1, 1, 2, 1, 1, 1)
    image[123] = 10
    image[148:173] = b"PatientName=DoNotExport\x00\x00\x00\x00"
    image[344:348] = b"n+1\x00"
    (directory / "Sub_001_scan.nii").write_bytes(image)
    policy = OutboundContextPolicy("workspace-alias-test-salt")
    dispatcher = build_work_dispatcher(
        get_run=lambda _: {},
        policy=policy,
        workspace_policy=WorkspaceFilePolicy(tmp_path),
        workspace_scope=("FunImg",),
    )
    listing = await dispatcher.dispatch(
        ToolCall(call_id="1", name="list_workspace_entries", arguments={"directory": "FunImg"})
    )
    alias = listing.result["entries"][0]["path"]
    assert "Sub_001" not in alias
    assert policy.redact_tool_result({"path": alias}).payload["path"] == alias
    files = await dispatcher.dispatch(
        ToolCall(call_id="2", name="list_workspace_entries", arguments={"directory": alias})
    )
    assert len(files.result["entries"]) == 2
    file_paths = {item["format"]: item["path"] for item in files.result["entries"]}
    json_result = await dispatcher.dispatch(
        ToolCall(call_id="3", name="read_workspace_content", arguments={"path": file_paths["json"]})
    )
    assert "RepetitionTime" in json_result.result["content"]
    header = await dispatcher.dispatch(
        ToolCall(
            call_id="4",
            name="read_workspace_content",
            arguments={"path": file_paths["image_header"]},
        )
    )
    assert header.result["dimensions"] == [3, 4, 5]
    assert header.result["volume_count"] == 6
    assert header.result["tr_seconds"] == 2
    assert "DoNotExport" not in str(header.result)
    escaped = await dispatcher.dispatch(
        ToolCall(call_id="5", name="read_workspace_content", arguments={"path": "../scan.nii"})
    )
    assert escaped.error_code is not None


def test_workspace_policy_rejects_escape_binary_and_hard_exclusion(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text("token=secret-value\n", encoding="utf-8")
    (tmp_path / "database.db").write_bytes(b"SQLite format 3")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.txt").write_text("hidden", encoding="utf-8")
    policy = WorkspaceFilePolicy(tmp_path)

    with pytest.raises(InputValidationError):
        policy.read("../safe.txt")
    with pytest.raises(InputValidationError):
        policy.read("database.db")
    with pytest.raises(InputValidationError):
        policy.read(".git/config.txt")
    assert "[redacted-secret]" in policy.read("safe.txt").content


def test_workspace_policy_builds_non_overlapping_atomic_patch(tmp_path: Path) -> None:
    target = tmp_path / "source.py"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    policy = WorkspaceFilePolicy(tmp_path)
    digest = policy.read("source.py").sha256
    proposal = policy.build_patch(
        [
            {
                "path": "source.py",
                "expected_sha256": digest,
                "start_line": 1,
                "end_line": 1,
                "replacement": "ONE\n",
            },
            {
                "path": "source.py",
                "expected_sha256": digest,
                "start_line": 4,
                "end_line": 4,
                "replacement": "FOUR\n",
            },
        ]
    )
    assert proposal["proposal_hash"]
    policy.apply_patch(proposal["edits"], tmp_path / "backups")
    assert target.read_text(encoding="utf-8") == "ONE\ntwo\nthree\nFOUR\n"
