"""User intent, metadata arithmetic and readable draft regressions."""

# ruff: noqa: RUF001

import json

from neuroagent.application.work_draft import WorkDraft, draft_snapshot, review_issues
from neuroagent.application.work_presentation import (
    constrained_patch,
    explicit_user_patch,
    observed_facts,
    semantic_patch,
)


def test_user_choices_survive_model_parent_replacement_and_named_enum_mapping():
    old = {"preprocessing": {"tr_seconds": 2, "normalization": {"mode": 2}}}
    patch, sources, blocked = constrained_patch(
        old,
        {"preprocessing.tr_seconds": "user", "preprocessing.normalization.mode": "user"},
        {"preprocessing": {"tr_seconds": 3, "normalization": None}},
        "解释一下标准化",
    )
    assert patch == old
    assert set(blocked) == {"preprocessing.tr_seconds", "preprocessing.normalization.mode"}
    assert sources["preprocessing.tr_seconds"] == "user"
    patch, _, blocked = constrained_patch(old, sources, {}, "TR改成3秒")
    assert patch == {"preprocessing": {"tr_seconds": 3}}
    assert not blocked
    assert semantic_patch(
        {
            "preprocessing": {
                "normalization": {"mode": "t1_segment"},
                "nuisance": {"head_motion_model": "friston_24"},
            }
        }
    ) == {"preprocessing": {"normalization": {"mode": 2}, "nuisance": {"head_motion_model": 4}}}


def test_explicit_choices_never_derive_from_negation_or_method_question():
    assert explicit_user_patch("是否使用 Segment，TR是2秒？") == {}
    assert "t1" not in explicit_user_patch("不要使用 Segment").get("preprocessing", {})
    assert "expected_time_points" not in explicit_user_patch("共33层").get("preprocessing", {})
    patch = explicit_user_patch("计算 ALFF，采用 T1 Segment，保留全部234个时间点，结果图平滑4 mm")
    assert patch["requested_metrics"] == ["alff"]
    assert patch["preprocessing"]["normalization"]["mode"] == 2
    assert patch["preprocessing"]["expected_time_points"] == 234
    assert patch["alff_falff"]["result_smoothing_fwhm_mm"] == [4, 4, 4]


def test_slice_order_is_calculated_from_successful_reads_and_conflicts_are_not_hidden():
    def read(output, status="succeeded"):
        return {"tool_name": "read_workspace_content", "status": status, "output": output}

    facts = observed_facts(
        {
            "calls": [
                read(
                    {
                        "format": "json",
                        "content": json.dumps(
                            {"RepetitionTime": 2, "SliceTiming": [0.5, 0, 0.5, 0.25]}
                        ),
                    }
                ),
                read(
                    {
                        "format": "image_header",
                        "tr_seconds": 2,
                        "volume_count": 234,
                        "dimensions": [64, 64, 4],
                    }
                ),
                read(
                    {
                        "format": "image_header",
                        "tr_seconds": 9,
                        "volume_count": 240,
                        "dimensions": [64, 64, 4],
                    },
                    "failed",
                ),
            ]
        }
    )
    assert facts["preprocessing.slice_timing.slice_order"] == [2, 4, 1, 3]
    assert facts["preprocessing.expected_time_points"] == 234
    draft = WorkDraft(preprocessing={"expected_time_points": 240, "tr_seconds": 2})
    assert any("240" in item and "234" in item for item in review_issues(draft, facts))
    snapshot = draft_snapshot(
        draft, workspace_path=None, sources={"preprocessing.tr_seconds": "user"}, facts=facts
    )
    assert snapshot["field_states"]["preprocessing.tr_seconds"] == "verified"
    assert snapshot["field_states"]["preprocessing.expected_time_points"] == "conflict"
    assert snapshot["sources"]["preprocessing.tr_seconds"] == "user"
    assert snapshot["version"] == 1
    changed = draft_snapshot(
        WorkDraft(preprocessing={"tr_seconds": 3}), workspace_path=None, sources={}, prior=snapshot
    )
    assert changed["version"] == 2
    assert {"path": "preprocessing.tr_seconds", "before": 2, "after": 3} in changed["changes"]


def test_differing_acquisitions_are_not_collapsed_to_the_last_header():
    calls = [
        {
            "tool_name": "read_workspace_content",
            "status": "succeeded",
            "output": {
                "format": "image_header",
                "tr_seconds": tr,
                "volume_count": 234,
                "dimensions": [64, 64, 33],
            },
        }
        for tr in (2, 3)
    ]
    assert "preprocessing.tr_seconds" not in observed_facts({"calls": calls})
