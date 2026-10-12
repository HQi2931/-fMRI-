from __future__ import annotations

# Full-width colon is the existing Chinese validator-message delimiter.
# ruff: noqa: RUF001
from neuroagent.application.planning_feedback import FeedbackDisposition
from neuroagent.application.preflight_adapter import (
    feedback_payload,
    planning_feedback_for_messages,
)


def test_authorized_missing_tr_is_a_read_candidate() -> None:
    feedback = planning_feedback_for_messages(
        ("preprocessing.tr_seconds：尚未提供",),
        namespace="preprocessing",
        draft_revision="draft-1",
        readable_targets=("preprocessing.tr_seconds",),
    )

    issue, decision = feedback[0]
    assert decision.disposition is FeedbackDisposition.READ_EVIDENCE
    assert issue.target == "preprocessing.tr_seconds"
    assert issue.missing_evidence == (issue.target,)
    assert issue.repairs[0].boundary.value == "read_only"


def test_unscoped_or_scientific_decision_is_asked_of_user() -> None:
    feedback = planning_feedback_for_messages(
        (
            "preprocessing.tr_seconds：尚未提供",
            "preprocessing.global_signal_regression：尚未提供",
        ),
        namespace="preprocessing",
        draft_revision="draft-1",
        # Global signal regression is a scientific choice and is deliberately
        # outside the read allowlist; TR is not authorized in this example.
        readable_targets=("preprocessing.global_signal_regression",),
    )

    assert [item[1].disposition for item in feedback] == [
        FeedbackDisposition.ASK_USER,
        FeedbackDisposition.ASK_USER,
    ]
    assert feedback[1][0].requires_user_decision


def test_conflicting_registered_fact_requires_user_and_marks_semantic_impact() -> None:
    feedback = planning_feedback_for_messages(
        ("preprocessing.tr_seconds：采集记录之间存在不同值",),
        namespace="preprocessing",
        draft_revision="draft-1",
        facts={
            "preprocessing.tr_seconds": {"conflicting_values": ["2.0", "2.5"]},
        },
        readable_targets=("preprocessing.tr_seconds",),
    )

    issue, decision = feedback[0]
    assert decision.disposition is FeedbackDisposition.EVIDENCE_CONFLICT
    assert issue.changes_scientific_semantics


def test_draft_repair_fingerprint_changes_with_draft_revision_and_serializes_safely() -> None:
    first = planning_feedback_for_messages(
        ("preprocessing.slice_timing.slice_count：必须大于 0",),
        namespace="preprocessing",
        draft_revision="draft-1",
    )
    changed = planning_feedback_for_messages(
        ("preprocessing.slice_timing.slice_count：必须大于 0",),
        namespace="preprocessing",
        draft_revision="draft-2",
    )

    assert first[0][1].disposition is FeedbackDisposition.REVALIDATE
    assert first[0][0].fingerprint != changed[0][0].fingerprint
    assert feedback_payload(first)[0]["repairs"][0]["boundary"] == "draft_only"
