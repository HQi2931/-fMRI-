from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from neuroagent.application.contracts.statistics import (
    StatisticalClusterRoiView,
)
from neuroagent.application.errors import ConflictError
from neuroagent.application.statistical_result_facts import build_method_summary
from neuroagent.domain.fmri.results import StatisticalAtlasSelection, StatisticalResultManifest
from tests.science.test_results_reporting import _real_manifest
from tests.science.test_statistical_result_facts import _detail


@pytest.mark.parametrize(
    "scenario", ["localized", "empty", "native", "unknown", "legacy", "blocked"]
)
def test_initial_model_facts_withhold_individual_values_and_distinguish_localization_states(
    service, monkeypatch, scenario
):
    manifest = _real_manifest()
    if scenario == "empty":
        values = manifest.model_dump(mode="python")
        values.update(clusters=(), inference_status=None, significant_voxel_count=None)
        manifest = StatisticalResultManifest.model_validate(values)
    elif scenario in {"native", "unknown"}:
        manifest = manifest.model_copy(
            update={
                "clusters": tuple(
                    item.model_copy(update={"coordinate_space": scenario})
                    for item in manifest.clusters
                )
            }
        )
    atlas = (
        None
        if scenario == "legacy"
        else StatisticalAtlasSelection(
            atlas_id="aal", atlas_name="AAL", atlas_content_hash="a" * 64, coordinate_space="MNI"
        )
    )
    detail = _detail(manifest=manifest, atlas=atlas)
    detail = detail.model_copy(update={"method_summary": build_method_summary(detail)})
    monkeypatch.setattr(service.runs, "get_run", lambda _: SimpleNamespace(project_id="project-a"))
    reads = []

    def result_list(*, project_id, run_id):
        assert (project_id, run_id) == ("project-a", "run-1")
        return [detail]

    monkeypatch.setattr(service.runs, "list_statistical_results", result_list)
    monkeypatch.setattr(service.runs, "get_statistical_result", lambda _: detail)

    def localize(result_id, atlas_id):
        reads.append(("localization", result_id, atlas_id))
        if scenario == "blocked":
            raise ConflictError("statistics_atlas_changed", "private source path must stay local")
        return SimpleNamespace(
            status="no_significant_voxels" if scenario == "empty" else "localized",
            model_dump=lambda **_: {
                "status": "no_significant_voxels" if scenario == "empty" else "localized",
                "localization_method": "inverse affine nearest neighbor",
                "clusters": [],
            },
        )

    def roi(result_id, cluster_id):
        reads.append(("roi", result_id, cluster_id))
        if scenario == "blocked":
            raise ConflictError("statistical_roi_inputs_changed", "private source detail")
        return StatisticalClusterRoiView(
            result_id=result_id,
            result_bundle_hash=detail.bundle_hash,
            cluster_id=cluster_id,
            status="no_significant_voxels" if scenario == "empty" else "descriptive",
            voxel_count=0 if scenario == "empty" else 12,
            rows=()
            if scenario == "empty"
            else ({"subject_id": "private-participant", "group": "HC", "mean_value": 3},),
        )

    monkeypatch.setattr(service.runs, "localize_statistical_result", localize)
    monkeypatch.setattr(service.runs, "statistical_cluster_roi", roi)
    basic = service._analysis_result_facts("run-1")
    assert "roi_summary" not in basic[0] and reads == []
    facts = service._analysis_result_facts("run-1", True)[0]
    expected = {
        "localized": "localized",
        "empty": "no_significant_voxels",
        "native": "coordinate_space_mismatch",
        "unknown": "coordinate_space_unknown",
        "legacy": "atlas_not_selected",
        "blocked": "blocked",
    }
    assert facts["localization"]["status"] == expected[scenario]
    assert any(item[0] == "localization" for item in reads) is (
        scenario in {"localized", "empty", "blocked"}
    )
    text = json.dumps(facts)
    assert "private" not in text and "subject_id" not in text and "mean_value" not in text
    assert "report_json" not in text and '"values"' not in text
    if scenario == "empty":
        assert facts["roi_summary"]["groups"] == []
    elif scenario == "blocked":
        assert facts["roi_summary"]["error_code"] == "statistical_roi_inputs_changed"
    else:
        assert facts["roi_summary"]["groups"][0]["mean"] == 3
        assert facts["roi_summary_cluster_selection"] == "first_reported_cluster"


def test_non_scientific_or_unverified_method_facts_do_not_extract_brain_or_roi(
    service, monkeypatch
):
    def forbidden(*args):
        raise AssertionError("scientific query must not run")

    monkeypatch.setattr(service.runs, "localize_statistical_result", forbidden)
    monkeypatch.setattr(service.runs, "statistical_cluster_roi", forbidden)
    for facts in (
        {"status": "method_summary_unavailable"},
        {"status": "succeeded", "non_scientific": True},
    ):
        before = facts.copy()
        service._add_statistical_spatial_facts(facts)
        assert facts == before
