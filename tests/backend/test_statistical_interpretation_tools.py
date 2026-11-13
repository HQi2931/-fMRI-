from __future__ import annotations

import asyncio
import json

import pytest

from neuroagent.agent.models import ToolCall
from neuroagent.agent.redaction import OutboundContextPolicy
from neuroagent.application.statistical_result_facts import build_method_summary
from neuroagent.application.work_tools import build_work_dispatcher, build_work_tool_bindings
from neuroagent.domain.fmri.results import StatisticalAtlasSelection, StatisticalResultManifest
from tests.science.test_results_reporting import _real_manifest
from tests.science.test_statistical_result_facts import _detail


def _callbacks():
    detail = _detail(
        atlas=StatisticalAtlasSelection(
            atlas_id="aal3", atlas_name="AAL3", atlas_content_hash="a" * 64, coordinate_space="MNI"
        )
    )
    summary = detail.model_copy(update={"method_summary": build_method_summary(detail)})
    calls = []

    def read_summary(result_id):
        calls.append(("summary", result_id))
        return summary.model_dump(mode="json")

    def localize(result_id, atlas_id):
        calls.append(("localization", result_id, atlas_id))
        return {
            "result_id": result_id,
            "result_bundle_hash": detail.bundle_hash,
            "status": "localized",
            "atlas_id": atlas_id,
            "clusters": [
                {
                    "cluster_id": "cluster-1",
                    "label": "Region",
                    "coverage": [
                        {"label": "Region", "label_value": 1, "voxel_count": 12, "proportion": 1.0}
                    ],
                }
            ],
        }

    def roi(result_id, cluster_id):
        calls.append(("roi", result_id, cluster_id))
        return {
            "result_id": result_id,
            "result_bundle_hash": detail.bundle_hash,
            "cluster_id": cluster_id,
            "status": "descriptive",
            "voxel_count": 12,
            "rows": [
                {"subject_id": "private-participant-1", "group": "ASD", "mean_value": 1},
                {"subject_id": "private-participant-2", "group": "ASD", "mean_value": 3},
            ],
        }

    return (
        detail,
        calls,
        dict(get_result_summary=read_summary, localize_result=localize, cluster_roi=roi),
    )


@pytest.mark.parametrize("wrong", ["project", "run"])
def test_result_queries_enforce_scope_before_loading_or_extracting_scientific_facts(wrong):
    detail, calls, callbacks = _callbacks()
    kwargs = {"project_id": "project-a", "allowed_run_ids": frozenset({"run-1"})}
    kwargs["project_id" if wrong == "project" else "allowed_run_ids"] = (
        "other-project" if wrong == "project" else frozenset({"other-run"})
    )
    bindings = build_work_tool_bindings(
        get_run=lambda _: {},
        get_result_detail=lambda _: detail.model_dump(mode="json"),
        work_agent_role="analysis",
        **kwargs,
        **callbacks,
    )
    for binding in bindings:
        if binding.tool.name in {
            "get_statistical_result_detail",
            "get_statistical_result_localization",
            "get_statistical_cluster_summary",
        }:
            assert (
                binding.handler({"result_id": "result-1", "cluster_id": "cluster-1"})["status"]
                == "not_available_in_scope"
            )
    assert calls == []


def test_analysis_dispatcher_injects_only_bounded_facts_and_descriptive_roi_aggregates():
    detail, calls, callbacks = _callbacks()
    dispatcher = build_work_dispatcher(
        get_run=lambda _: {},
        get_result_detail=lambda _: detail.model_dump(mode="json"),
        policy=OutboundContextPolicy("interpretation-tool-test-redaction-salt"),
        project_id="project-a",
        allowed_run_ids=frozenset({"run-1"}),
        work_agent_role="analysis",
        **callbacks,
    )
    outputs = []
    for name, arguments in (
        ("get_statistical_result_detail", {"result_id": "result-1"}),
        ("get_statistical_result_localization", {"result_id": "result-1"}),
        ("get_statistical_cluster_summary", {"result_id": "result-1", "cluster_id": "cluster-1"}),
    ):
        outcome = asyncio.run(
            dispatcher.dispatch(ToolCall(call_id=name, name=name, arguments=arguments))
        )
        assert outcome.status.value == "succeeded"
        outputs.append(outcome.result)
    encoded = json.dumps(outputs, ensure_ascii=False)
    assert "private-participant" not in encoded and "sub-01" not in encoded
    assert (
        "report_json" not in encoded and "mean_value" not in encoded and '"values"' not in encoded
    )
    assert '"mean": 2' in encoded and '"count": 2' in encoded
    assert ("localization", "result-1", "aal3") in calls


def test_preprocessing_role_cannot_invoke_analysis_fact_tools():
    detail, calls, callbacks = _callbacks()
    dispatcher = build_work_dispatcher(
        get_run=lambda _: {},
        get_result_detail=lambda _: detail.model_dump(mode="json"),
        policy=OutboundContextPolicy("interpretation-tool-test-redaction-salt"),
        work_agent_role="preprocessing",
        **callbacks,
    )
    for name in (
        "get_statistical_result_detail",
        "get_statistical_result_localization",
        "get_statistical_cluster_summary",
    ):
        outcome = asyncio.run(
            dispatcher.dispatch(
                ToolCall(call_id=name, name=name, arguments={"result_id": "result-1"})
            )
        )
        assert outcome.error_code == "unknown_tool"
    assert calls == []


@pytest.mark.parametrize(
    "space,status",
    [
        ("native", "coordinate_space_mismatch"),
        ("unknown", "coordinate_space_unknown"),
        ("MNI", "atlas_selection_required"),
        ("empty", "no_significant_voxels"),
    ],
)
def test_localization_tool_keeps_empty_and_unlocatable_results_distinct(space, status):
    values = _real_manifest().model_dump(mode="python")
    if space == "empty":
        values.update(clusters=(), inference_status=None, significant_voxel_count=None)
    else:
        values["clusters"] = tuple(
            item.model_copy(update={"coordinate_space": space})
            for item in _real_manifest().clusters
        )
    detail = _detail(manifest=StatisticalResultManifest.model_validate(values))
    calls = []

    def localize(result_id, atlas_id):
        calls.append((result_id, atlas_id))
        return {"status": "no_significant_voxels", "clusters": [], "significant_voxel_count": 0}

    bindings = build_work_tool_bindings(
        get_run=lambda _: {},
        get_result_detail=lambda _: detail.model_dump(mode="json"),
        localize_result=localize,
        project_id="project-a",
        allowed_run_ids=frozenset({"run-1"}),
        work_agent_role="analysis",
    )
    binding = next(
        item for item in bindings if item.tool.name == "get_statistical_result_localization"
    )
    assert binding.handler({"result_id": "result-1"})["status"] == status
    assert calls == ([("result-1", "")] if space == "empty" else [])
