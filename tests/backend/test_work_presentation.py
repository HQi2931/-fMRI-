"""User intent, metadata arithmetic and readable draft regressions."""

# ruff: noqa: RUF001

import json

from neuroagent.application.work_draft import (
    WorkDraft,
    draft_snapshot,
    merge_work_draft,
    review_issues,
)
from neuroagent.application.work_presentation import (
    constrained_patch,
    explicit_user_patch,
    merge_observed_facts,
    observed_facts,
    pending_review_questions,
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
    assert (
        explicit_user_patch("采用 Old Segment")["preprocessing"]["t1"]["segmentation"] == "segment"
    )
    assert "segmentation" not in explicit_user_patch("不要使用 Old Segment").get(
        "preprocessing", {}
    ).get("t1", {})


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
    facts = observed_facts({"calls": calls})
    assert facts["preprocessing.tr_seconds"] == {"conflicting_values": ["2", "3"]}
    draft = WorkDraft(preprocessing={"tr_seconds": 2})
    assert any("读取的输入存在不同值" in issue for issue in review_issues(draft, facts))
    snapshot = draft_snapshot(draft, workspace_path=None, sources={}, facts=facts)
    assert snapshot["field_states"]["preprocessing.tr_seconds"] == "conflict"


def test_cross_turn_acquisition_conflicts_survive_partial_rereads():
    key = "preprocessing.tr_seconds"
    initial = {key: 2, "preprocessing.expected_time_points": 234}
    conflict = merge_observed_facts(initial, {key: 3})
    assert conflict[key] == {"conflicting_values": ["2", "3"]}
    assert merge_observed_facts(conflict, {key: 2}) == conflict
    assert merge_observed_facts(conflict, {}) == conflict
    assert merge_observed_facts(initial, {key: 2.0}) == initial
    assert initial[key] == 2
    draft = WorkDraft(preprocessing={"tr_seconds": 2})
    assert any("读取的输入存在不同值" in issue for issue in review_issues(draft, conflict))


def test_invalid_header_numbers_do_not_become_verified_facts():
    def read(tr, count, dimensions):
        return {
            "tool_name": "read_workspace_content",
            "status": "succeeded",
            "output": {
                "format": "image_header",
                "tr_seconds": tr,
                "volume_count": count,
                "dimensions": dimensions,
            },
        }

    for tr in (True, -2, 0, float("nan"), float("inf"), "2"):
        assert observed_facts({"calls": [read(tr, 234, [64])]}) == {
            "preprocessing.expected_time_points": 234
        }
    for count in (True, "234", None, 0):
        assert observed_facts({"calls": [read(2, count, [64, 64, 33])]}) == {}
    for slices in (True, -1, None, "33"):
        facts = observed_facts({"calls": [read(2, 234, [64, 64, slices])]})
        assert "preprocessing.slice_timing.slice_count" not in facts


def test_anatomical_sidecar_tr_does_not_contaminate_functional_facts():
    def read(path, content):
        return {
            "tool_name": "read_workspace_content",
            "status": "succeeded",
            "output": {"format": "json", "path": path, "content": json.dumps(content)},
        }

    facts = observed_facts(
        {
            "calls": [
                read("T1Img/anon/anat.json", {"RepetitionTime": 0.008}),
                read("FunImg/anon/rest.json", {"RepetitionTime": 2}),
                read("FunImg/anon/invalid.json", {"RepetitionTime": True}),
                read("FunImg/anon/invalid2.json", {"RepetitionTime": float("nan")}),
            ]
        }
    )
    assert facts == {"preprocessing.tr_seconds": 2}


def test_questions_are_bound_to_unresolved_parameters_not_repeated_known_choices():
    values = {
        "study_protocol_ref": "protocol-test",
        "preprocessing": {"tr_seconds": 2, "dummy_scans": 0},
        "alff_falff": {"result_smoothing_fwhm_mm": [4, 4, 4]},
    }
    questions = [
        {"text": "TR 使用 2 秒吗？", "parameter_paths": ["preprocessing.tr_seconds"]},
        "结果图需要平滑为 4 mm 吗？",
        {"text": "删除初始时间点吗？", "parameter_paths": ["preprocessing.dummy_scans"]},
        {"text": "采用哪个掩膜？", "parameter_paths": ["alff_falff.mask_source"]},
        "采用哪个掩膜？",
    ]
    assert pending_review_questions(
        questions,
        values,
        {
            "study_protocol_ref": "user",
            "alff_falff.result_smoothing_fwhm_mm": "user",
            "preprocessing.dummy_scans": "user",
        },
        {"preprocessing.tr_seconds": 2},
    ) == ["采用哪个掩膜？"]


def test_user_choice_does_not_hide_a_question_about_conflicting_read_evidence():
    path = "preprocessing.tr_seconds"
    question = {"text": "采集 TR 与方案不同，如何处理？", "parameter_paths": [path]}
    for fact in (3, {"conflicting_values": ["2", "3"]}):
        assert pending_review_questions(
            [question],
            {"study_protocol_ref": "protocol-test", "preprocessing": {"tr_seconds": 2}},
            {path: "user"},
            {path: fact},
        ) == [question["text"]]


def test_explicit_no_filter_and_no_presmoothing_clear_model_active_slots():
    proposed = {
        "preprocessing": {
            "temporal_filter": {
                "timing": "disabled",
                "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
                "add_mean_back": True,
            },
            "smoothing": {"timing": "disabled", "method": "gaussian", "fwhm_mm": [4, 4, 4]},
        }
    }
    patch, sources, blocked = constrained_patch(
        {}, {}, proposed, "计算 ALFF，计算前不带通滤波、不空间平滑，回归中加回均值"
    )
    assert patch["preprocessing"]["temporal_filter"] == {
        "timing": "disabled",
        "frequency_band": None,
        "add_mean_back": None,
    }
    assert patch["preprocessing"]["smoothing"] == {
        "timing": "disabled",
        "method": None,
        "fwhm_mm": None,
    }
    assert patch["preprocessing"]["nuisance"]["add_mean_back"] is True
    assert "preprocessing.temporal_filter.add_mean_back" in blocked
    assert sources["preprocessing.temporal_filter.frequency_band"] == "user"


def test_segment_normalization_reuses_selected_t1_without_overriding_explicit_input():
    draft = WorkDraft(
        preprocessing={"normalization": {"mode": 2}, "t1": {"segmentation": "segment"}}
    )
    merged = merge_work_draft(draft, {})
    assert (
        merged.preprocessing["normalization"]["structural_artifact_id"]
        == "workspace:anatomical-source"
    )
    assert "structural_artifact_id" not in draft.preprocessing["normalization"]
    for explicit in (None, "explicit-other-artifact"):
        changed = merge_work_draft(
            draft, {"preprocessing": {"normalization": {"structural_artifact_id": explicit}}}
        )
        assert changed.preprocessing["normalization"]["structural_artifact_id"] == explicit
    no_t1 = merge_work_draft(
        WorkDraft(preprocessing={"normalization": {"mode": 2}, "t1": None}), {}
    )
    assert "structural_artifact_id" not in no_t1.preprocessing["normalization"]


def test_complete_protocol_explicit_choices_are_protected_from_later_model_replacement():
    text = (
        "只计算 ALFF，保留全部234个时间点，采用 T1 Segment 标准化到 MNI 3 mm。"
        "已有 T1 是裁剪好的结构像，直接使用并配准到功能像，仿射正则化采用 MNI。"
        "标准化功能像，范围为[-90,-126,-72]至[90,90,108]；标准化后做 Friston-24、"
        "白质和脑脊液均值回归。组织掩膜来自本次 Segment、阈值均0.9，包含一次线性趋势、"
        "加回均值，不做全局信号回归，不把掩膜再变回个体空间，不剔除时间点，不另做线性去趋势。"
        "计算前不带通滤波、不空间平滑，结果图平滑4 mm，使用 DPABI 自动掩膜，生成头动 QC。"
        "输出原始 ALFF、全脑均值归一化和 z 标准化结果。"
    )
    patch, sources, _ = constrained_patch({}, {}, {}, text)
    assert patch["preprocessing"]["dummy_scans"] == 0
    assert patch["preprocessing"]["normalization"]["voxel_size_mm"] == [3, 3, 3]
    assert patch["preprocessing"]["normalization"]["bounding_box_mm"] == [
        [-90, -126, -72],
        [90, 90, 108],
    ]
    assert patch["preprocessing"]["nuisance"]["white_matter"]["mask_threshold"] == 0.9
    assert patch["qc_mode"] == "head_motion"
    assert patch["alff_falff"]["requested_scalings"] == ["raw", "global_mean", "z_score"]
    retained, _, blocked = constrained_patch(
        patch,
        sources,
        {"qc_mode": "skip", "preprocessing": {"normalization": {"voxel_size_mm": [6, 6, 6]}}},
        "解释当前方案",
    )
    assert retained["qc_mode"] == "head_motion"
    assert retained["preprocessing"]["normalization"]["voxel_size_mm"] == [3, 3, 3]
    assert set(blocked) == {"qc_mode", "preprocessing.normalization.voxel_size_mm"}


def test_explicit_bet_and_skipping_manual_t1_reorientation():
    patch = explicit_user_patch("跳过 T1 重定向，只做 BET 和 T1 与功能像配准，保留 Segment")
    assert patch["preprocessing"]["t1"]["preparation"] == "prepared"
    assert patch["preprocessing"]["t1"]["bet"] is True
    assert (
        explicit_user_patch("T1 coreg to fun")["preprocessing"]["t1"]["coregister_to_functional"]
        is True
    )
    assert explicit_user_patch("不要 BET")["preprocessing"]["t1"]["bet"] is False
    assert explicit_user_patch("启用BET去颅骨")["preprocessing"]["t1"]["bet"] is True
    assert explicit_user_patch("只做Bet和T1 coreg to fun")["preprocessing"]["t1"]["bet"] is True
    assert explicit_user_patch("不想做BET")["preprocessing"]["t1"]["bet"] is False
    assert "bet" not in explicit_user_patch("BET是什么？").get("preprocessing", {}).get("t1", {})


def test_reho_only_smoothing_change_does_not_create_an_alff_branch():
    patch, sources, _ = constrained_patch(
        {"requested_metrics": ["reho"], "reho": {"smooth_reho_fwhm_mm": [4, 4, 4]}},
        {},
        {},
        "结果图平滑改为6 mm",
    )
    assert "alff_falff" not in patch
    from neuroagent.domain.fmri.metrics import RehoParameters

    assert set(patch["reho"]) <= RehoParameters.model_fields.keys()
    assert patch["reho"]["smooth_reho_fwhm_mm"] == [6, 6, 6]
    assert sources["reho.smooth_reho_fwhm_mm"] == "user"


def test_inactive_completion_keeps_scientific_unknowns_and_conflicts_visible():
    from neuroagent.application.work_presentation import complete_inactive_parameters

    completed = complete_inactive_parameters(
        {
            "preprocessing": {
                "realignment": {"enabled": True},
                "temporal_filter": {
                    "timing": "disabled",
                    "frequency_band": {"low_hz": 0.01, "high_hz": 0.08},
                },
                "nuisance": {"white_matter": {"enabled": True, "method": "mean"}},
            },
            "alff_falff": {"mask_source": "dpabi_auto", "result_smoothing": False},
        }
    )
    assert completed["alff_falff"]["mask_artifact_id"] is None
    assert completed["alff_falff"]["result_smoothing_fwhm_mm"] is None
    assert "options_source" not in completed["preprocessing"]["realignment"]
    assert completed["preprocessing"]["temporal_filter"]["frequency_band"] is not None
    tissue = completed["preprocessing"]["nuisance"]["white_matter"]
    assert tissue["compcor_components"] is None
    assert "mask_threshold" not in tissue


def test_full_alff_protocol_keeps_other_negated_steps_separate_from_metric_intent():
    patch = explicit_user_patch(
        "完成一次 ALFF，去噪采用 Friston-24、白质和脑脊液均值回归、线性趋势，"
        "不做全局信号回归并保留均值，ALFF频段0.01–0.08 Hz。"
    )
    assert patch["requested_metrics"] == ["alff"]
    nuisance = patch["preprocessing"]["nuisance"]
    assert nuisance["head_motion_model"] == 4
    assert nuisance["global_signal"]["enabled"] is False
    assert nuisance["polynomial_trend"] == 1
    assert nuisance["white_matter"]["method"] == "mean"
    assert nuisance["csf"]["method"] == "mean"
    assert patch["alff_falff"]["frequency_band"] == {"low_hz": 0.01, "high_hz": 0.08}
