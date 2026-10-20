from __future__ import annotations

from neuroagent.application.contracts.plans import ValidationIssue

# Full-width colon is the existing Chinese validator-message delimiter.
# ruff: noqa: RUF001
from neuroagent.application.planning_feedback import FeedbackDisposition
from neuroagent.application.preflight_adapter import (
    feedback_payload,
    planning_feedback_for_messages,
    planning_feedback_for_skill_issues,
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


def test_observed_fact_fingerprint_is_stable_for_same_draft_and_changes_with_draft() -> None:
    arguments = {
        "messages": ("preprocessing.expected_time_points：需要复核候选值",),
        "namespace": "preprocessing",
        "facts": {"preprocessing.expected_time_points": 240},
    }
    first = planning_feedback_for_messages(**arguments, draft_revision="draft-1")
    repeated = planning_feedback_for_messages(**arguments, draft_revision="draft-1")
    changed = planning_feedback_for_messages(**arguments, draft_revision="draft-2")

    assert first[0][0].fingerprint == repeated[0][0].fingerprint
    assert first[0][0].fingerprint != changed[0][0].fingerprint

    stopped = planning_feedback_for_messages(
        **arguments,
        draft_revision="draft-1",
        prior_fingerprints=(first[0][0].fingerprint,),
    )
    progressed = planning_feedback_for_messages(
        **arguments,
        draft_revision="draft-2",
        prior_fingerprints=(first[0][0].fingerprint,),
    )
    assert stopped[0][1].disposition is FeedbackDisposition.NO_PROGRESS
    assert progressed[0][1].disposition is FeedbackDisposition.REVALIDATE


def test_skill_blockers_keep_typed_disposition_and_same_evidence_fingerprint() -> None:
    blocker = ValidationIssue(
        code="EFFECTIVE_VOLUME_COUNT_UNKNOWN",
        message="The number of retained volumes is not verified",
        severity="blocking",
        path="preprocessing.tr_seconds",
    )
    first = planning_feedback_for_skill_issues(
        (blocker,),
        draft_revision="draft-hash",
        manifest_revision="manifest-hash",
    )
    repeated = planning_feedback_for_skill_issues(
        (blocker,),
        draft_revision="draft-hash",
        manifest_revision="manifest-hash",
        prior_fingerprints=(first[0][0].fingerprint,),
    )

    issue, feedback = first[0]
    assert issue.kind.value == "scientific_decision_required"
    assert issue.retryable is False
    assert feedback.disposition is FeedbackDisposition.ASK_USER
    assert repeated[0][0].fingerprint == issue.fingerprint
    assert repeated[0][1].disposition is FeedbackDisposition.ASK_USER


def test_skill_compiler_failure_is_capability_block_not_user_method_choice() -> None:
    feedback = planning_feedback_for_skill_issues(
        (
            ValidationIssue(
                code="SKILL_COMPILE_FAILED",
                message="compiler could not compose supported steps",
                severity="blocking",
                path="steps",
            ),
        ),
        draft_revision="draft-hash",
        manifest_revision="manifest-hash",
    )

    issue, decision = feedback[0]
    assert issue.kind.value == "unsupported_capability"
    assert issue.requires_user_decision is False
    assert decision.disposition is FeedbackDisposition.BLOCKED_CAPABILITY


def test_skill_dicom_inventory_and_package_governance_are_capability_blocks() -> None:
    feedback = planning_feedback_for_skill_issues(
        (
            ValidationIssue(
                code="MANIFEST_DICOM_INVENTORY_ONLY",
                message="DICOM conversion is not registered",
                severity="blocking",
                path="manifest",
            ),
            ValidationIssue(
                code="SCIENTIFIC_SKILL_WITHOUT_METHODOLOGIST_REVIEW",
                message="A reviewed scientific Skill lacks required review",
                severity="blocking",
                path="skill",
            ),
        ),
        draft_revision="draft-hash",
        manifest_revision="manifest-hash",
    )

    assert [item[1].disposition for item in feedback] == [
        FeedbackDisposition.BLOCKED_CAPABILITY,
        FeedbackDisposition.BLOCKED_CAPABILITY,
    ]


def test_preprocessing_session_scope_is_a_capability_boundary() -> None:
    feedback = planning_feedback_for_skill_issues(
        (
            ValidationIssue(
                code="MANIFEST_PREPROCESSING_SESSION_UNSUPPORTED",
                message="当前预处理不支持跨 session 合并。请分别整理输入。",
                severity="blocking",
                path="manifest.subjects.session_id",
            ),
        ),
        draft_revision="draft-hash",
        manifest_revision="manifest-hash",
    )

    issue, decision = feedback[0]
    assert issue.kind.value == "unsupported_capability"
    assert issue.target == "manifest.subjects.session_id"
    assert issue.message.endswith("请分别整理输入。")
    assert issue.repairs[0].action.value == "none"
    assert decision.disposition is FeedbackDisposition.BLOCKED_CAPABILITY


def test_result_normalization_mask_space_limit_is_a_capability_boundary() -> None:
    feedback = planning_feedback_for_skill_issues(
        (
            ValidationIssue(
                code="RESULT_NORMALIZATION_METRIC_MASK_SPACES_UNSUPPORTED",
                message="当前执行合同只有一个指标掩膜绑定。",
                severity="blocking",
                path="preprocessing.normalization.timing",
                remediation="请由研究者决定是否调整方案后重新预检。",
            ),
        ),
        draft_revision="draft-hash",
        manifest_revision="manifest-hash",
    )

    issue, decision = feedback[0]
    assert issue.kind.value == "unsupported_capability"
    assert issue.target == "preprocessing.normalization.timing"
    assert issue.requires_user_decision is False
    assert issue.repairs[0].action.value == "none"
    assert "请由研究者决定" in issue.message
    assert decision.disposition is FeedbackDisposition.BLOCKED_CAPABILITY


def test_result_stage_without_metric_limit_is_a_capability_boundary() -> None:
    feedback = planning_feedback_for_skill_issues(
        (
            ValidationIssue(
                code="RESULT_STAGE_OPERATION_WITHOUT_METRIC_UNSUPPORTED",
                message="当前执行器要求结果阶段操作对应已请求的指标输出。",
                severity="blocking",
                path="preprocessing.normalization.timing",
                remediation="请停用结果阶段操作后重新预检。",
            ),
            ValidationIssue(
                code="RESULT_STAGE_OPERATION_WITHOUT_METRIC_UNSUPPORTED",
                message="当前执行器要求结果阶段操作对应已请求的指标输出。",
                severity="blocking",
                path="preprocessing.smoothing.timing",
                remediation="请停用结果阶段操作后重新预检。",
            ),
        ),
        draft_revision="draft-hash",
        manifest_revision="manifest-hash",
    )

    assert len(feedback) == 2
    for issue, decision in feedback:
        assert issue.kind.value == "unsupported_capability"
        assert issue.requires_user_decision is False
        assert issue.repairs[0].action.value == "none"
        assert decision.disposition is FeedbackDisposition.BLOCKED_CAPABILITY


def test_reho_cut_checkpoint_limit_is_a_capability_boundary() -> None:
    feedback = planning_feedback_for_skill_issues(
        (
            ValidationIssue(
                code="REHO_CUT_CHECKPOINT_UNSUPPORTED",
                message="CUT preprocessing and ReHo require a verified checkpoint.",
                severity="blocking",
                path="preprocessing.scrubbing.method",
                remediation="Complete and verify the checkpoint before planning ReHo.",
            ),
        ),
        draft_revision="draft-hash",
        manifest_revision="manifest-hash",
    )

    issue, decision = feedback[0]
    assert issue.kind.value == "unsupported_capability"
    assert issue.requires_user_decision is False
    assert issue.repairs[0].action.value == "none"
    assert decision.disposition is FeedbackDisposition.BLOCKED_CAPABILITY


def test_unknown_skill_blocker_fails_closed_without_claiming_scientific_choice() -> None:
    feedback = planning_feedback_for_skill_issues(
        (
            ValidationIssue(
                code="FUTURE_SKILL_CONTRACT_BLOCKER",
                message="An unclassified contract issue occurred",
                severity="blocking",
                path="skill_plan",
            ),
        ),
        draft_revision="draft-hash",
        manifest_revision="manifest-hash",
    )

    issue, decision = feedback[0]
    assert issue.kind.value == "invalid_draft"
    assert issue.requires_user_decision is True
    assert issue.changes_scientific_semantics is False
    assert decision.disposition is FeedbackDisposition.ASK_USER
