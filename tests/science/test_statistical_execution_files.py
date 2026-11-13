from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from neuroagent.execution.matlab import MatlabTemplateRenderer
from neuroagent.infrastructure.matlab_executor import MatlabJobExecutor

from .test_execution_edges import statistics_job
from .test_matlab_execution import environment


@pytest.fixture
def rendered_statistics(tmp_path):
    spec = statistics_job()
    root = tmp_path / "work" / spec.run_id
    for binding in spec.artifact_bindings:
        path = root / binding.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(binding.artifact_id.encode("utf-8"))
    rendered = MatlabTemplateRenderer(Path("matlab/templates")).render(
        spec, environment(tmp_path), root.parent
    )
    return spec, rendered


def test_statistics_registers_exact_rendered_scripts_configuration_and_staged_hashes(
    rendered_statistics,
):
    spec, rendered = rendered_statistics
    original = rendered.entry_script.read_bytes()
    rows = MatlabJobExecutor._statistical_reproducibility_artifacts(rendered.run_directory, spec)
    assert {row["artifact_type"] for row in rows} == {
        f"statistics.{role}"
        for role in (
            "execution_script",
            "execution_bootstrap",
            "job_configuration",
            "input_manifest",
        )
    }
    for row in rows:
        data = (rendered.run_directory / row["relative_path"]).read_bytes()
        assert row["checksum"] == hashlib.sha256(data).hexdigest()
        assert row["size_bytes"] == len(data) and row["provenance"]["job_hash"] == spec.job_hash
    assert original == rendered.entry_script.read_bytes()
    manifest = json.loads((rendered.run_directory / "config/input-manifest.json").read_text())
    assert manifest["plan_hash"] == spec.plan_hash
    assert manifest["input_manifest_hash"] == spec.input_manifest_hash
    assert [row["artifact_id"] for row in manifest["inputs"]] == [
        binding.artifact_id for binding in spec.artifact_bindings
    ]
    for row in manifest["inputs"]:
        expected = row["artifact_id"].encode("utf-8")
        assert row["sha256"] == hashlib.sha256(expected).hexdigest()
        assert row["size_bytes"] == len(expected)
    assert (
        MatlabJobExecutor._statistical_reproducibility_artifacts(rendered.run_directory, spec)
        == rows
    )


@pytest.mark.parametrize(
    "damage", ["missing_script", "empty_script", "changed_input", "script_link", "config_link"]
)
def test_incomplete_or_changed_execution_files_cannot_be_registered(
    rendered_statistics, monkeypatch, damage
):
    spec, rendered = rendered_statistics
    MatlabJobExecutor._statistical_reproducibility_artifacts(rendered.run_directory, spec)
    if damage == "missing_script":
        rendered.entry_script.unlink()
    elif damage == "empty_script":
        rendered.entry_script.write_bytes(b"")
    elif damage == "changed_input":
        (rendered.run_directory / spec.artifact_bindings[0].relative_path).write_bytes(b"changed")
    elif damage == "script_link":
        original = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink", lambda path: path.name == "run_statistics.m" or original(path)
        )
    else:
        original = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink", lambda path: path.name == "config" or original(path)
        )
    with pytest.raises((OSError, ValueError)):
        MatlabJobExecutor._statistical_reproducibility_artifacts(rendered.run_directory, spec)
