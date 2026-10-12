"""Deterministic planning feedback contracts for Work preflight.

This module intentionally has no dependency on WorkDialogue, persistence, or an
executor. It describes a validation result and decides whether another bounded
read/validation pass can make progress; callers remain responsible for tools,
budgets, and authorization.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256


class PlanningIssueKind(StrEnum):
    MISSING_EVIDENCE = "missing_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    INVALID_DRAFT = "invalid_draft"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    SCIENTIFIC_DECISION_REQUIRED = "scientific_decision_required"
    AUTHORIZATION_REQUIRED = "authorization_required"


class EvidenceStatus(StrEnum):
    EXECUTOR_VERIFIED = "executor_verified"
    USER_DECLARED = "user_declared"
    CANDIDATE_INFERENCE = "candidate_inference"
    UNKNOWN = "unknown"


class EvidenceSourceKind(StrEnum):
    FILE_METADATA = "file_metadata"
    EXECUTION_RECORD = "execution_record"
    PROJECT_PROTOCOL = "project_protocol"
    USER_INPUT = "user_input"
    MODEL_INFERENCE = "model_inference"
    UNKNOWN = "unknown"


class RepairAction(StrEnum):
    READ_REGISTERED_SOURCE = "read_registered_source"
    REFRESH_STALE_EVIDENCE = "refresh_stale_evidence"
    PATCH_UNAPPROVED_DRAFT = "patch_unapproved_draft"
    ASK_USER = "ask_user"
    NONE = "none"


class RepairBoundary(StrEnum):
    READ_ONLY = "read_only"
    DRAFT_ONLY = "draft_only"
    USER_DECISION = "user_decision"
    UNSUPPORTED = "unsupported"


class FeedbackDisposition(StrEnum):
    READ_EVIDENCE = "read_evidence"
    REVALIDATE = "revalidate"
    ASK_USER = "ask_user"
    EVIDENCE_CONFLICT = "evidence_conflict"
    BLOCKED_CAPABILITY = "blocked_capability"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RETRY_LIMIT = "retry_limit"
    NO_PROGRESS = "no_progress"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A fact reference with explicit provenance and confidence class."""

    key: str
    source: EvidenceSourceKind
    status: EvidenceStatus
    revision: str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("evidence key must not be empty")


@dataclass(frozen=True, slots=True)
class RepairCandidate:
    """An allow-listed next action; free-form commands are not representable."""

    action: RepairAction
    boundary: RepairBoundary
    target: str

    def __post_init__(self) -> None:
        if not self.target.strip():
            raise ValueError("repair target must not be empty")
        expected = {
            RepairAction.READ_REGISTERED_SOURCE: RepairBoundary.READ_ONLY,
            RepairAction.REFRESH_STALE_EVIDENCE: RepairBoundary.READ_ONLY,
            RepairAction.PATCH_UNAPPROVED_DRAFT: RepairBoundary.DRAFT_ONLY,
            RepairAction.ASK_USER: RepairBoundary.USER_DECISION,
            RepairAction.NONE: RepairBoundary.UNSUPPORTED,
        }[self.action]
        if self.boundary is not expected:
            raise ValueError(f"{self.action.value} requires {expected.value} boundary")


@dataclass(frozen=True, slots=True)
class PlanningIssue:
    """Structured, user-explainable result of deterministic preflight."""

    code: str
    kind: PlanningIssueKind
    target: str
    message: str
    missing_evidence: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    repairs: tuple[RepairCandidate, ...] = ()
    changes_scientific_semantics: bool = False
    requires_user_decision: bool = False
    retryable: bool = False

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.target.strip() or not self.message.strip():
            raise ValueError("issue code, target, and message must not be empty")
        if len(set(self.missing_evidence)) != len(self.missing_evidence):
            raise ValueError("missing_evidence entries must be unique")
        if self.requires_user_decision and not any(
            repair.action is RepairAction.ASK_USER for repair in self.repairs
        ):
            raise ValueError("user decision issues must include an ASK_USER repair")
        if self.changes_scientific_semantics and not self.requires_user_decision:
            raise ValueError("scientific semantic changes must require user decision")
        if self.kind is PlanningIssueKind.SCIENTIFIC_DECISION_REQUIRED:
            if not self.requires_user_decision:
                raise ValueError("scientific decisions must require user input")
            if not self.changes_scientific_semantics:
                raise ValueError("scientific decision must declare semantic impact")
        if self.kind is PlanningIssueKind.UNSUPPORTED_CAPABILITY and self.retryable:
            raise ValueError("unsupported capability cannot be retried without new capability")

    @property
    def fingerprint(self) -> str:
        """Stable identity of the same issue against the same evidence revision."""
        body = {
            "code": self.code,
            "kind": self.kind.value,
            "target": self.target,
            "missing": sorted(self.missing_evidence),
            "evidence": sorted(
                (
                    (item.key, item.source.value, item.status.value, item.revision)
                    for item in self.evidence
                ),
                key=lambda item: (item[0], item[1], item[2], item[3] or ""),
            ),
        }
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PreflightFeedback:
    disposition: FeedbackDisposition
    issue: PlanningIssue | None = None
    reason: str = ""


def decide_feedback(
    issue: PlanningIssue | None,
    *,
    prior_fingerprints: Iterable[str] = (),
    newly_available_evidence: bool = False,
) -> PreflightFeedback:
    """Choose one safe next step without performing tools or changing a draft.

    Repeating an identical issue against identical evidence is a no-progress
    stop, unless a successful read has supplied new evidence for revalidation.
    """
    if issue is None:
        return PreflightFeedback(FeedbackDisposition.READY, reason="preflight passed")

    if issue.kind is PlanningIssueKind.UNSUPPORTED_CAPABILITY:
        return PreflightFeedback(
            FeedbackDisposition.BLOCKED_CAPABILITY,
            issue,
            "required capability is outside the registered implementation",
        )
    if issue.kind is PlanningIssueKind.CONFLICTING_EVIDENCE:
        return PreflightFeedback(
            FeedbackDisposition.EVIDENCE_CONFLICT,
            issue,
            "conflicting evidence requires resolution before another model proposal",
        )
    if issue.requires_user_decision:
        return PreflightFeedback(
            FeedbackDisposition.ASK_USER,
            issue,
            "a scientific or authorization decision belongs to the user",
        )
    if newly_available_evidence:
        return PreflightFeedback(
            FeedbackDisposition.REVALIDATE,
            issue,
            "new evidence is available; validate the unchanged draft again",
        )
    if issue.fingerprint in set(prior_fingerprints):
        return PreflightFeedback(
            FeedbackDisposition.NO_PROGRESS,
            issue,
            "same issue and evidence revision recurred",
        )
    if issue.retryable and any(
        repair.action in {RepairAction.READ_REGISTERED_SOURCE, RepairAction.REFRESH_STALE_EVIDENCE}
        for repair in issue.repairs
    ):
        return PreflightFeedback(
            FeedbackDisposition.READ_EVIDENCE,
            issue,
            "registered evidence can be read before asking the user",
        )
    if any(repair.action is RepairAction.PATCH_UNAPPROVED_DRAFT for repair in issue.repairs):
        return PreflightFeedback(
            FeedbackDisposition.REVALIDATE,
            issue,
            "an allow-listed unapproved draft patch may be proposed then checked",
        )
    if any(repair.action is RepairAction.ASK_USER for repair in issue.repairs):
        return PreflightFeedback(FeedbackDisposition.ASK_USER, issue, "user input is required")
    return PreflightFeedback(
        FeedbackDisposition.BLOCKED_CAPABILITY,
        issue,
        "no registered safe repair candidate is available",
    )
