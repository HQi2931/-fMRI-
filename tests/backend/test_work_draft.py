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


def test_review_contains_every_nested_choice_and_hash_is_order_independent() -> None:
    draft = complete_draft()
    review = render_draft_review(draft)
    assert draft_hash(draft) in review
    for key in draft.preprocessing:
        assert key in review
    assert "enabled" in review
    assert "tr_seconds" in review
    preview = plan_preview(draft)
    assert preview["kind"] == "dpabi_plan_preview"
    assert preview["provisional"] is True
    assert preview["status"] == "ready_for_review"
    assert preview["common_steps"]
    reordered = draft.model_copy(
        update={"preprocessing": dict(reversed(list(draft.preprocessing.items())))}
    )
    assert draft_hash(reordered) == draft_hash(draft)


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
