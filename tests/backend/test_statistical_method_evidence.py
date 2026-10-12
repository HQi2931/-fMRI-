from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from neuroagent.application.service_mixins import runs
from neuroagent.domain.fmri.results import StatisticalArtifactRole
from tests.science.test_results_reporting import _artifact, _real_manifest
from tests.science.test_statistical_result_facts import _detail


@pytest.fixture
def evidence_fixture(service, tmp_path, monkeypatch):
    manifest = _real_manifest()
    correction_meta = _artifact(StatisticalArtifactRole.CORRECTION_EVIDENCE)
    manifest = manifest.model_copy(update={"artifacts": (*manifest.artifacts, correction_meta)})
    detail = _detail(manifest=manifest)
    correction = tmp_path / "correction.json"
    correction.write_text(
        json.dumps(
            {
                "method": "fdr",
                "q_threshold": 0.05,
                "statistic_type": "T",
                "df1": 2,
                "df2": [],
                "actual_p_threshold": 0.004,
            }
        ),
        encoding="utf-8",
    )
    software = tmp_path / "software.json"
    software.write_text(
        json.dumps({"matlab_version": "test-version", "dpabi": "test-dpabi", "path": "private"}),
        encoding="utf-8",
    )
    artifacts = {
        meta.artifact_id: SimpleNamespace(
            artifact_id=meta.artifact_id, checksum=meta.checksum_sha256, size_bytes=meta.size_bytes
        )
        for meta in manifest.artifacts
    }
    monkeypatch.setattr(service.repository, "get_statistical_result", lambda _: detail)
    monkeypatch.setattr(service.repository, "get_artifact", lambda key: artifacts[key])
    monkeypatch.setattr(service.repository, "get_run", lambda _: SimpleNamespace())
    monkeypatch.setattr(service.repository, "get_project", lambda _: SimpleNamespace())
    monkeypatch.setattr(
        runs,
        "artifact_content_path",
        lambda item, *_: (
            correction if item.artifact_id == correction_meta.artifact_id else software
        ),
    )
    return detail, correction, software, artifacts[correction_meta.artifact_id]


def test_verified_registered_evidence_distinguishes_fdr_q_from_actual_p(service, evidence_fixture):
    result = service.runs.get_statistical_result("result-1")
    summary = result.method_summary
    assert summary.correction_evidence_status == "verified"
    assert summary.q_threshold == 0.05 and summary.actual_p_threshold == 0.004
    assert summary.software_versions["MATLAB"] == "test-version"
    assert "private" not in summary.model_dump_json()
    assert summary.evidence_issues == ()


@pytest.mark.parametrize("corruption", ["checksum", "size", "oversize", "array", "json", "missing"])
def test_unreadable_registered_evidence_is_explicitly_unavailable(
    service, evidence_fixture, corruption
):
    _, correction, _, artifact = evidence_fixture
    if corruption == "checksum":
        artifact.checksum = "f" * 64
    elif corruption == "size":
        artifact.size_bytes += 1
    elif corruption == "oversize":
        correction.write_text(" " * 256_001, encoding="utf-8")
    elif corruption == "array":
        correction.write_text("[]", encoding="utf-8")
    elif corruption == "json":
        correction.write_text("invalid", encoding="utf-8")
    else:
        correction.unlink()
    result = service.runs.get_statistical_result("result-1")
    assert result.method_summary.correction_evidence_status == "unavailable"
    assert result.method_summary.actual_p_threshold is None
    assert result.method_summary.evidence_issues == ("correction_evidence_unavailable",)
    # The recorded report is still readable without fabricating the missing value.
    assert result.report_markdown


@pytest.mark.parametrize("tamper", ["method", "df", "q", "bundle"])
def test_semantically_conflicting_evidence_withholds_model_method_facts(
    service, evidence_fixture, monkeypatch, tamper
):
    detail, correction, _, _ = evidence_fixture
    if tamper == "bundle":
        monkeypatch.setattr(
            service.repository,
            "get_statistical_result",
            lambda _: detail.model_copy(update={"bundle_hash": "f" * 64}),
        )
    else:
        payload = json.loads(correction.read_text(encoding="utf-8"))
        payload[{"method": "method", "df": "df1", "q": "q_threshold"}[tamper]] = {
            "method": "grf",
            "df": 99,
            "q": 0.01,
        }[tamper]
        correction.write_text(json.dumps(payload), encoding="utf-8")
    result = service.runs.get_statistical_result("result-1")
    assert result.method_summary is None and result.report_markdown


def test_legacy_result_reports_unregistered_correction_without_hiding_other_method_facts(
    service, monkeypatch
):
    detail = _detail()
    monkeypatch.setattr(service.repository, "get_statistical_result", lambda _: detail)
    monkeypatch.setattr(
        service.repository,
        "get_artifact",
        lambda _: (_ for _ in ()).throw(ValueError("legacy software absent")),
    )
    result = service.runs.get_statistical_result("result-1")
    assert result.method_summary.correction_evidence_status == "not_registered"
    assert result.method_summary.actual_p_threshold is None
    assert result.method_summary.evidence_issues == ("software_version_evidence_unavailable",)
