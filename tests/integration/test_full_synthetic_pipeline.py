from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest


def _load_demo_module() -> ModuleType:
    script = Path(__file__).resolve().parents[2] / "scripts" / "synthetic-demo.py"
    spec = importlib.util.spec_from_file_location("rsfmri_synthetic_demo", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load synthetic demo script")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.integration
def test_full_synthetic_pipeline_is_auditable_and_non_scientific(tmp_path: Path) -> None:
    module = _load_demo_module()
    run_demo = cast(Callable[[Path], dict[str, Any]], module.run_demo)

    summary = run_demo(tmp_path / "demo")

    assert (tmp_path / "demo" / "synthetic-demo.sqlite").is_file()
    assert summary["synthetic"] is True
    assert summary["scientific_use"] == "prohibited"
    assert "NON-SCIENTIFIC" in summary["notice"]
    assert summary["source"] == {
        "read_only_placeholders": True,
        "unchanged_during_pipeline": True,
        "file_count": 7,
    }
    assert summary["dataset"]["subject_ids"] == ["sub-01", "sub-02", "sub-03"]
    assert len(summary["dataset"]["manifest_hash"]) == 64

    assert summary["plans"]["input"]["state"] == "approved"
    assert summary["plans"]["alff"]["state"] == "approved"
    assert summary["plans"]["alff"]["skill_ids"] == ["rsfmri.metric.alff_falff"]
    assert summary["plans"]["statistics"] == {
        "plan_revision_id": summary["plans"]["statistics"]["plan_revision_id"],
        "state": "approved",
        "test": "one_sample_t",
        "correction": "fdr",
    }

    assert summary["runs"]["input"]["state"] == "qc_review"
    assert summary["runs"]["alff"]["state"] == "succeeded"
    assert summary["runs"]["statistics"]["state"] == "succeeded"
    assert {run["executor"] for run in summary["runs"].values()} == {"mock"}
    assert summary["qc"]["state"] == "approved"
    assert summary["qc"]["included_subject_ids"] == ["sub-01", "sub-02", "sub-03"]

    assert len(summary["artifacts"]["alff_metric_artifact_ids"]) == 3
    assert {
        "synthetic.non_scientific.mask.verified",
        "synthetic.non_scientific.timeseries.verified",
    }.issubset(summary["artifacts"]["input_types"])
    assert summary["artifacts"]["alff_types"].count("synthetic.non_scientific.alff") == 3
    assert set(summary["artifacts"]["statistics_types"]) == {
        "mock.result",
        "synthetic.non_scientific.report.json",
        "synthetic.non_scientific.report.markdown",
    }
    assert "test-only" in summary["artifacts"]["injection_mode"]

    report = summary["report"]
    assert report["mode"] == "synthetic_non_scientific"
    assert report["scientific_use"] == "prohibited"
    assert len(report["bundle_hash"]) == 64
    assert report["formats"] == ["json", "markdown"]
    registered_result = summary["statistical_result"]
    assert registered_result["result_id"] == "synthetic-non-scientific-statistical-result"
    assert registered_result["mode"] == "synthetic_non_scientific"
    assert registered_result["non_scientific"] is True
    assert registered_result["bundle_hash"] == report["bundle_hash"]
    report_root = (
        tmp_path / "demo" / "work" / "project" / "reports" / summary["runs"]["statistics"]["run_id"]
    )
    assert (report_root / "synthetic-report.json").is_file()
    assert "SYNTHETIC / NON-SCIENTIFIC RESULT" in (report_root / "synthetic-report.md").read_text(
        encoding="utf-8"
    )

    serialized = json.dumps(summary, sort_keys=True).lower()
    for forbidden_claim in ("t_value", "p_value", "significant", "cluster_table"):
        assert forbidden_claim not in serialized
