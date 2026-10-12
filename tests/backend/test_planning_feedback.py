from __future__ import annotations

from dataclasses import replace

import pytest

from neuroagent.application.planning_feedback import (
    EvidenceRef,
    EvidenceSourceKind,
    EvidenceStatus,
    FeedbackDisposition,
    PlanningIssue,
    PlanningIssueKind,
    RepairAction,
    RepairBoundary,
    RepairCandidate,
    decide_feedback,
)


def test_missing_tr_can_be_read_from_registered_metadata_before_user_question() -> None:
    issue = PlanningIssue(
        code="missing_tr",
        kind=PlanningIssueKind.MISSING_EVIDENCE,
        target="preprocessing.tr_seconds",
        message="TR is required to compile this preprocessing plan.",
        missing_evidence=("repetition_time",),
        evidence=(
            EvidenceRef(
                "nifti.header.repetition_time",
                EvidenceSourceKind.FILE_METADATA,
                EvidenceStatus.EXECUTOR_VERIFIED,
                "header-sha256:abc",
            ),
        ),
        repairs=(
            RepairCandidate(
                RepairAction.READ_REGISTERED_SOURCE,
                RepairBoundary.READ_ONLY,
                "nifti.header.repetition_time",
            ),
        ),
        retryable=True,
    )

    result = decide_feedback(issue)

    assert result.disposition is FeedbackDisposition.READ_EVIDENCE
    assert result.issue is issue


def test_declared_unknown_and_inferred_evidence_are_distinct() -> None:
    values = {
        EvidenceStatus.EXECUTOR_VERIFIED,
        EvidenceStatus.USER_DECLARED,
        EvidenceStatus.CANDIDATE_INFERENCE,
        EvidenceStatus.UNKNOWN,
    }
    assert len(values) == 4
    assert EvidenceRef("tr", EvidenceSourceKind.USER_INPUT, EvidenceStatus.USER_DECLARED)


def test_same_issue_and_same_evidence_stops_but_new_revision_can_revalidate() -> None:
    base = dict(
        code="invalid_bandpass",
        kind=PlanningIssueKind.INVALID_DRAFT,
        target="preprocessing.bandpass",
        message="The requested bandpass is invalid.",
        evidence=(
            EvidenceRef(
                "draft.parameters",
                EvidenceSourceKind.USER_INPUT,
                EvidenceStatus.USER_DECLARED,
                "r1",
            ),
        ),
        repairs=(
            RepairCandidate(
                RepairAction.PATCH_UNAPPROVED_DRAFT, RepairBoundary.DRAFT_ONLY, "bandpass"
            ),
        ),
        retryable=True,
    )
    issue = PlanningIssue(**base)
    same_issue = PlanningIssue(**base)

    assert issue.fingerprint == same_issue.fingerprint
    assert (
        decide_feedback(same_issue, prior_fingerprints=[issue.fingerprint]).disposition
        is FeedbackDisposition.NO_PROGRESS
    )
    assert (
        decide_feedback(
            same_issue,
            prior_fingerprints=[issue.fingerprint],
            newly_available_evidence=True,
        ).disposition
        is FeedbackDisposition.REVALIDATE
    )
    assert (
        PlanningIssue(
            **{
                **base,
                "evidence": (
                    EvidenceRef(
                        "draft.parameters",
                        EvidenceSourceKind.USER_INPUT,
                        EvidenceStatus.USER_DECLARED,
                        "r2",
                    ),
                ),
            }
        ).fingerprint
        != issue.fingerprint
    )


def test_scientific_method_choice_is_user_decision_and_cannot_be_automated() -> None:
    issue = PlanningIssue(
        code="choose_gsr",
        kind=PlanningIssueKind.SCIENTIFIC_DECISION_REQUIRED,
        target="preprocessing.global_signal_regression",
        message="Project protocol does not specify whether to use GSR.",
        repairs=(
            RepairCandidate(RepairAction.ASK_USER, RepairBoundary.USER_DECISION, "GSR policy"),
        ),
        changes_scientific_semantics=True,
        requires_user_decision=True,
        retryable=False,
    )

    assert decide_feedback(issue).disposition is FeedbackDisposition.ASK_USER


def test_unsupported_fc_is_capability_boundary_not_retryable_science_rule() -> None:
    issue = PlanningIssue(
        code="fc_not_registered",
        kind=PlanningIssueKind.UNSUPPORTED_CAPABILITY,
        target="analysis.fc",
        message="Functional connectivity is not registered by this executor.",
        repairs=(RepairCandidate(RepairAction.NONE, RepairBoundary.UNSUPPORTED, "analysis.fc"),),
    )

    assert decide_feedback(issue).disposition is FeedbackDisposition.BLOCKED_CAPABILITY
    with pytest.raises(ValueError, match="cannot be retried"):
        PlanningIssue(
            code="fc_not_registered",
            kind=PlanningIssueKind.UNSUPPORTED_CAPABILITY,
            target="analysis.fc",
            message="Not registered.",
            retryable=True,
        )


def test_free_form_or_semantically_unsafe_repair_cannot_be_encoded() -> None:
    with pytest.raises(ValueError, match="requires read_only boundary"):
        RepairCandidate(RepairAction.READ_REGISTERED_SOURCE, RepairBoundary.DRAFT_ONLY, "source")
    with pytest.raises(ValueError, match="must include an ASK_USER"):
        PlanningIssue(
            code="decision",
            kind=PlanningIssueKind.SCIENTIFIC_DECISION_REQUIRED,
            target="method",
            message="Select method.",
            changes_scientific_semantics=True,
            requires_user_decision=True,
        )


def test_fingerprint_evidence_order_is_deterministic_with_unknown_revision() -> None:
    left = PlanningIssue(
        code="conflict",
        kind=PlanningIssueKind.CONFLICTING_EVIDENCE,
        target="tr",
        message="Conflicting TR evidence.",
        evidence=(
            EvidenceRef("tr", EvidenceSourceKind.FILE_METADATA, EvidenceStatus.EXECUTOR_VERIFIED),
            EvidenceRef(
                "tr", EvidenceSourceKind.FILE_METADATA, EvidenceStatus.EXECUTOR_VERIFIED, "r1"
            ),
        ),
    )
    right = replace(left, evidence=tuple(reversed(left.evidence)))
    assert left.fingerprint == right.fingerprint
