"""Draft reviews preserve explicit scientific choices and user confirmation."""

import pytest
from pydantic import ValidationError

from neuroagent.application.work_draft import (
    WorkDraft,
    confirmed_skill_intent,
    draft_hash,
    draft_issues,
    draft_schema,
    merge_work_draft,
    plan_preview,
    planning_decisions,
    render_draft_review,
)
from tests.science.conftest import preprocessing_parameters


def complete_draft() -> WorkDraft:
    values = preprocessing_parameters().model_dump(mode="json")
    values.pop("provenance")
    return WorkDraft(
        preprocessing=values,
        primary_outputs=["preprocessed_bold"],
        study_protocol_ref="test:reviewed-protocol",
        qc_mode="skip",
    )


def test_partial_merge_preserves_values_and_clears_confirmation() -> None:
    draft = WorkDraft(preprocessing={"nuisance": {"enabled": True, "timing": "after_realign"}})
    draft.confirmed_hash = draft_hash(draft)
    changed = merge_work_draft(draft, {"preprocessing": {"nuisance": {"enabled": False}}})
    assert changed.preprocessing["nuisance"] == {"enabled": False, "timing": "after_realign"}
    assert changed.confirmed_hash is None
    assert draft.preprocessing["nuisance"]["enabled"] is True
    changed = merge_work_draft(changed, {"preprocessing": {"nuisance": {"timing": None}}})
    assert changed.preprocessing["nuisance"]["timing"] is None
    assert merge_work_draft(draft, {}).confirmed_hash == draft.confirmed_hash


def test_t1_symbolic_source_binds_only_at_user_confirmation() -> None:
    draft = merge_work_draft(
        complete_draft(),
        {
            "preprocessing": {
                "normalization": {
                    "mode": 3,
                    "structural_artifact_id": "workspace:anatomical-source",
                },
                "t1": {
                    "preparation": "reorient_crop",
                    "coregister_to_functional": True,
                    "segmentation": "new_segment",
                    "affine_regularization": "mni",
                    "dartel": True,
                },
            }
        },
    )
    intent = confirmed_skill_intent(
        draft,
        project_id="project",
        dataset_ref="dataset",
        input_manifest_hash="a" * 64,
        reviewed_hash=draft_hash(draft),
        user_confirmation_ref="user-confirmation",
        structural_artifact_id="manifest:server-manifest:anatomical-source",
    )
    assert (
        intent.preprocessing.t1.structural_artifact_id
        == "manifest:server-manifest:anatomical-source"
    )
    assert draft.preprocessing["t1"]["structural_artifact_id"] == "workspace:anatomical-source"


def test_model_cannot_supply_confirmation_or_provenance() -> None:
    with pytest.raises(ValueError, match="确认"):
        merge_work_draft(WorkDraft(), {"confirmed_hash": "forged"})
    with pytest.raises(ValidationError, match="来源"):
        merge_work_draft(WorkDraft(), {"preprocessing": {"provenance": []}})


def test_empty_draft_and_invalid_scientific_choices_are_actionable() -> None:
    issues = "\n".join(draft_issues(WorkDraft()))
    assert "tr_seconds" in issues
    assert "qc_mode" in issues
    assert "primary_outputs" in issues
    assert "provenance" not in issues
    invalid = merge_work_draft(complete_draft(), {"preprocessing": {"tr_seconds": -1}})
    assert any("tr_seconds" in item for item in draft_issues(invalid))


def test_confirmation_binds_review_and_generates_user_evidence() -> None:
    draft = complete_draft()
    assert not draft_issues(draft)
    arguments = {
        "project_id": "server-project",
        "dataset_ref": "server-dataset",
        "input_manifest_hash": "a" * 64,
        "reviewed_hash": draft_hash(draft),
        "user_confirmation_ref": "user-message-42",
    }
    intent = confirmed_skill_intent(draft, **arguments)
    assert intent.project_id == "server-project"
    assert intent.preprocessing is not None
    assert all(p.source == "user" for p in intent.preprocessing.provenance)
    assert all("user-message-42" in p.evidence_ref for p in intent.preprocessing.provenance)
    with pytest.raises(ValueError, match="重新审阅"):
        confirmed_skill_intent(merge_work_draft(draft, {"qc_mode": "head_motion"}), **arguments)
    arguments["user_confirmation_ref"] = ""
    with pytest.raises(ValueError, match="消息引用"):
        confirmed_skill_intent(draft, **arguments)


def test_review_is_concise_and_full_choices_remain_structured() -> None:
    draft = complete_draft()
    review = render_draft_review(draft)
    assert draft_hash(draft) not in review
    assert "enabled" not in review
    assert "tr_seconds" not in review
    assert "TR 2.0 秒" in review
    assert len(review) < 500
    assert all(key in draft.model_dump()["preprocessing"] for key in draft.preprocessing)
    preview = plan_preview(draft)
    assert preview["kind"] == "dpabi_plan_preview"
    assert preview["provisional"] is True
    assert preview["status"] == "ready_for_review"
    assert preview["common_steps"]
    reordered = draft.model_copy(
        update={"preprocessing": dict(reversed(list(draft.preprocessing.items())))}
    )
    assert draft_hash(reordered) == draft_hash(draft)


def test_preview_labels_prepared_t1_and_disabled_smoothing_accurately() -> None:
    preview = plan_preview(
        WorkDraft(
            preprocessing={"t1": {"preparation": "prepared"}, "smoothing": {"timing": "disabled"}}
        )
    )
    labels = [item["label"] for item in preview["common_steps"]]
    assert "使用已准备的 T1 结构像" in labels
    assert "公共功能像平滑停用" in labels
    assert not any("裁剪" in label for label in labels)


def test_plan_preview_keeps_alff_and_reho_checkpoints_separate() -> None:
    draft = WorkDraft(
        preprocessing={"smoothing": {"timing": "disabled"}},
        requested_metrics=["alff", "reho"],
        alff_falff={
            "filter_timing": "disabled",
            "result_smoothing": False,
        },
        reho={
            "temporal_filter_band": {"low_hz": 0.01, "high_hz": 0.08},
            "smooth_reho": False,
            "global_result_smoothing": False,
        },
    )

    branches = {item["branch_id"]: item for item in plan_preview(draft)["branches"]}
    assert branches["alff_falff"]["consumes"].startswith("未带通滤波")
    assert branches["reho"]["consumes"].startswith("未空间平滑")
    assert branches["reho"]["steps"] == [
        "prepare_reho_temporal_filter_checkpoint",
        "calculate_reho",
        "register_reho",
    ]


def test_planning_decisions_separate_user_edits_suggestions_and_conflicts() -> None:
    draft = WorkDraft(
        preprocessing={"tr_seconds": -1, "dummy_scans": 10},
        requested_metrics=["alff", "reho"],
        alff_falff={"frequency_band": {"low_hz": 0.01, "high_hz": 0.08}},
        reho={"cluster_voxels": 27},
    )
    report = planning_decisions(
        draft,
        {"preprocessing.tr_seconds": "user", "reho.cluster_voxels": "suggested"},
    )
    choices = {item["path"]: item for item in report["choices"]}
    assert report["provisional"] is True
    assert report["branches"] == ["alff_falff", "reho"]
    assert choices["preprocessing.tr_seconds"]["status"] == "conflict"
    assert choices["preprocessing.dummy_scans"]["status"] == "proposed"
    assert choices["preprocessing.expected_time_points"]["status"] == "missing"
    assert choices["reho.cluster_voxels"]["status"] == "proposed"
    assert choices["reho.cluster_voxels"]["source"] == "suggested"
    assert "核实" in report["note"]
    review = render_draft_review(draft, {"reho.cluster_voxels": "suggested"})
    assert "reho.cluster_voxels" not in review
    assert "方案区域" in review

    corrected = planning_decisions(
        WorkDraft(preprocessing={"tr_seconds": 2.0}),
        {"preprocessing.tr_seconds": "user"},
    )
    assert (
        next(item for item in corrected["choices"] if item["path"] == "preprocessing.tr_seconds")[
            "status"
        ]
        == "user_draft"
    )


def test_plan_preview_survives_partial_or_malformed_nested_draft() -> None:
    preview = plan_preview(WorkDraft(preprocessing={"smoothing": None}))
    assert preview["status"] == "incomplete"
    assert preview["common_steps"][-1]["status"] == "disabled_or_pending"


def test_schema_does_not_expose_provenance_or_confirmation() -> None:
    import json

    schema = json.dumps(draft_schema())
    assert "provenance" not in schema
    assert "confirmed_hash" not in schema
    assert "slice_order" in schema
