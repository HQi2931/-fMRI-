"""Application adapters from existing validator messages to typed feedback."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from hashlib import sha256
from typing import Any

from neuroagent.application.contracts.plans import ValidationIssue
from neuroagent.application.planning_feedback import (
    EvidenceRef,
    EvidenceSourceKind,
    EvidenceStatus,
    PlanningIssue,
    PlanningIssueKind,
    PreflightFeedback,
    RepairAction,
    RepairBoundary,
    RepairCandidate,
    decide_feedback,
)
from neuroagent.application.statistics_draft import DesignIssue

_READABLE_METADATA_TARGETS = frozenset(
    {
        "preprocessing.tr_seconds",
        "preprocessing.expected_time_points",
        "preprocessing.slice_timing.slice_count",
        "preprocessing.slice_timing.slice_order",
    }
)

_SKILL_CAPABILITY_BLOCKER_CODES = frozenset(
    {
        "SKILL_COMPILE_FAILED",
        "SKILL_NOT_AVAILABLE",
        "UNSUPPORTED_METRIC_REQUEST",
        "MANIFEST_DICOM_INVENTORY_ONLY",
        "MANIFEST_PREPROCESSING_SESSION_UNSUPPORTED",
        "RESULT_NORMALIZATION_METRIC_MASK_SPACES_UNSUPPORTED",
        "REVIEWED_SKILL_WITHOUT_EVIDENCE",
        "SCIENTIFIC_SKILL_WITHOUT_METHODOLOGIST_REVIEW",
    }
)
_SKILL_SCIENTIFIC_BLOCKER_CODES = frozenset(
    {
        "EFFECTIVE_VOLUME_COUNT_UNKNOWN",
        "FREQUENCY_BAND_BELOW_EFFECTIVE_RESOLUTION",
        "FALFF_INPUT_ALREADY_FILTERED",
        "ALFF_INPUT_SPATIALLY_SMOOTHED",
        "ALFF_FILTER_TIMING_BEFORE_NORMALIZE",
        "REHO_INPUT_SPATIALLY_SMOOTHED",
        "REHO_INPUT_ALREADY_FILTERED",
        "METRIC_PREPROCESSING_SMOOTHING_CONFLICT",
        "FALFF_PREPROCESSING_FILTERED_INPUT",
        "REHO_PREPROCESSING_PREFILTERED_INPUT",
    }
)


def planning_feedback_for_messages(
    messages: Iterable[str],
    *,
    namespace: str,
    draft_revision: str,
    facts: Mapping[str, Any] | None = None,
    sources: Mapping[str, str] | None = None,
    prior_fingerprints: Iterable[str] = (),
    newly_available_targets: Iterable[str] = (),
    readable_targets: Iterable[str] = (),
) -> tuple[tuple[PlanningIssue, PreflightFeedback], ...]:
    """Classify bounded app-validator messages without guessing scientific facts.

    A field can be proposed for repair only while it remains an unapproved
    draft. Known acquisition metadata is offered as a READ candidate only when
    the caller supplies an already-authorized read scope.
    """

    known_facts = facts or {}
    known_sources = sources or {}
    prior = tuple(prior_fingerprints)
    newly_available = set(newly_available_targets)
    readable = set(readable_targets) & _READABLE_METADATA_TARGETS
    results: list[tuple[PlanningIssue, PreflightFeedback]] = []

    for message in messages:
        if not isinstance(message, str) or not message.strip():
            continue
        target, detail = _target_and_detail(message, namespace)
        fact = known_facts.get(target)
        conflict = isinstance(fact, dict) and "conflicting_values" in fact
        missing = _is_missing_message(message)
        evidence = (_evidence_for(target, fact, known_sources, draft_revision),)

        if conflict:
            repair = RepairCandidate(RepairAction.ASK_USER, RepairBoundary.USER_DECISION, target)
            issue = PlanningIssue(
                code=_issue_code(namespace, target, "conflicting_evidence"),
                kind=PlanningIssueKind.CONFLICTING_EVIDENCE,
                target=target,
                message=detail,
                evidence=evidence,
                repairs=(repair,),
                changes_scientific_semantics=True,
                requires_user_decision=True,
            )
        elif missing:
            if target in readable:
                repairs = (
                    RepairCandidate(
                        RepairAction.READ_REGISTERED_SOURCE, RepairBoundary.READ_ONLY, target
                    ),
                )
                retryable = True
                requires_user_decision = False
            elif namespace != "preprocessing":
                # Analysis may propose a method from the user's stated goal, but
                # it remains an unapproved draft and the role prompt forbids
                # filling scientific defaults without evidence.
                repairs = (
                    RepairCandidate(
                        RepairAction.PATCH_UNAPPROVED_DRAFT,
                        RepairBoundary.DRAFT_ONLY,
                        target,
                    ),
                )
                retryable = True
                requires_user_decision = False
            else:
                repairs = (
                    RepairCandidate(RepairAction.ASK_USER, RepairBoundary.USER_DECISION, target),
                )
                retryable = False
                requires_user_decision = True
            issue = PlanningIssue(
                code=_issue_code(namespace, target, "missing_evidence"),
                kind=PlanningIssueKind.MISSING_EVIDENCE,
                target=target,
                message=detail,
                missing_evidence=(target,),
                evidence=evidence,
                repairs=repairs,
                requires_user_decision=requires_user_decision,
                retryable=retryable,
            )
        else:
            issue = PlanningIssue(
                code=_issue_code(namespace, target, "invalid_draft"),
                kind=PlanningIssueKind.INVALID_DRAFT,
                target=target,
                message=detail,
                evidence=evidence,
                repairs=(
                    RepairCandidate(
                        RepairAction.PATCH_UNAPPROVED_DRAFT,
                        RepairBoundary.DRAFT_ONLY,
                        target,
                    ),
                ),
                retryable=True,
            )

        decision = decide_feedback(
            issue,
            prior_fingerprints=prior,
            newly_available_evidence=target in newly_available,
        )
        results.append((issue, decision))
    return tuple(results)


def feedback_payload(
    items: Iterable[tuple[PlanningIssue, PreflightFeedback]],
) -> list[dict[str, Any]]:
    """Convert feedback to bounded JSON data for persistence and model context."""

    result: list[dict[str, Any]] = []
    for issue, feedback in items:
        result.append(
            {
                "code": issue.code,
                "kind": issue.kind.value,
                "target": issue.target,
                "message": issue.message,
                "missing_evidence": list(issue.missing_evidence),
                "evidence": [
                    {
                        "key": item.key,
                        "source": item.source.value,
                        "status": item.status.value,
                        "revision": item.revision,
                    }
                    for item in issue.evidence
                ],
                "repairs": [
                    {
                        "action": item.action.value,
                        "boundary": item.boundary.value,
                        "target": item.target,
                    }
                    for item in issue.repairs
                ],
                "changes_scientific_semantics": issue.changes_scientific_semantics,
                "requires_user_decision": issue.requires_user_decision,
                "retryable": issue.retryable,
                "fingerprint": issue.fingerprint,
                "disposition": feedback.disposition.value,
                "disposition_reason": feedback.reason,
            }
        )
    return result


def planning_feedback_for_skill_issues(
    issues: Iterable[ValidationIssue],
    *,
    draft_revision: str,
    manifest_revision: str,
    prior_fingerprints: Iterable[str] = (),
) -> tuple[tuple[PlanningIssue, PreflightFeedback], ...]:
    """Map typed Skill blockers directly; never classify their prose as patches."""
    results: list[tuple[PlanningIssue, PreflightFeedback]] = []
    for item in issues:
        if item.severity != "blocking":
            continue
        target = item.path if item.path and _SAFE_TARGET.fullmatch(item.path) else "skill_plan"
        unsupported = item.code in _SKILL_CAPABILITY_BLOCKER_CODES
        conflict = item.code.endswith("_AMBIGUOUS") or "_MISMATCH" in item.code
        if unsupported:
            kind = PlanningIssueKind.UNSUPPORTED_CAPABILITY
            repair = RepairCandidate(RepairAction.NONE, RepairBoundary.UNSUPPORTED, target)
            requires_user = False
            changes_semantics = False
        elif conflict:
            kind = PlanningIssueKind.CONFLICTING_EVIDENCE
            repair = RepairCandidate(RepairAction.ASK_USER, RepairBoundary.USER_DECISION, target)
            requires_user = True
            changes_semantics = True
        elif item.code in _SKILL_SCIENTIFIC_BLOCKER_CODES:
            kind = PlanningIssueKind.SCIENTIFIC_DECISION_REQUIRED
            repair = RepairCandidate(RepairAction.ASK_USER, RepairBoundary.USER_DECISION, target)
            requires_user = True
            changes_semantics = True
        else:
            # Unknown Skill blockers still stop for review, but their code does
            # not prove that the user must choose a scientific method.
            kind = PlanningIssueKind.INVALID_DRAFT
            repair = RepairCandidate(RepairAction.ASK_USER, RepairBoundary.USER_DECISION, target)
            requires_user = True
            changes_semantics = False
        issue = PlanningIssue(
            code=f"skill_preflight_{item.code.casefold()}",
            kind=kind,
            target=target,
            message=(f"{item.message} {item.remediation}" if item.remediation else item.message),
            missing_evidence=(target,)
            if kind is PlanningIssueKind.SCIENTIFIC_DECISION_REQUIRED
            else (),
            evidence=(
                EvidenceRef(
                    "work_draft",
                    EvidenceSourceKind.MODEL_INFERENCE,
                    EvidenceStatus.CANDIDATE_INFERENCE,
                    draft_revision,
                ),
                EvidenceRef(
                    "input_manifest",
                    EvidenceSourceKind.FILE_METADATA,
                    EvidenceStatus.EXECUTOR_VERIFIED,
                    manifest_revision,
                ),
            ),
            repairs=(repair,),
            changes_scientific_semantics=changes_semantics,
            requires_user_decision=requires_user,
            retryable=False,
        )
        results.append(
            (
                issue,
                decide_feedback(issue, prior_fingerprints=prior_fingerprints),
            )
        )
    return tuple(results)


def planning_feedback_for_design_issues(
    issues: Iterable[DesignIssue],
    *,
    draft_revision: str,
    prior_fingerprints: Iterable[str] = (),
) -> tuple[tuple[PlanningIssue, PreflightFeedback], ...]:
    """Map statistical dry-run results to fail-closed Work feedback.

    The assembler's prose is never fed through the generic message classifier:
    known source-revision conflicts and unsupported capabilities retain their
    typed meaning, known scientific-design failures ask the user, and unknown
    blockers stop for user review instead of inviting the model to patch them.
    """

    results: list[tuple[PlanningIssue, PreflightFeedback]] = []
    prior = tuple(prior_fingerprints)
    capability_codes = frozenset(
        {"statistics_capability_not_validated", "skill_parameter_schema_invalid"}
    )
    scientific_codes = frozenset(
        {
            "design_rejected_by_domain",
            "correction_rejected_by_domain",
            "invalid_correction_for_design",
            "statistics_choice_unresolved",
        }
    )
    for item in issues:
        safe_code = item.code if _SAFE_TARGET.fullmatch(item.code) else "statistics_dry_run"
        missing: tuple[str, ...] = ()
        target = (
            "input_manifest"
            if "manifest" in item.code
            else "registered_artifacts"
            if "artifact" in item.code or "mask" in item.code
            else "qc_decision"
            if "qc_" in item.code
            else "demographics_revision"
            if "demographics" in item.code
            else "external_import_revision"
            if "external_import" in item.code
            else "project_revision"
            if "project" in item.code
            else "statistical_source"
            if "source" in item.code
            else safe_code[:181]
        )
        if item.code in capability_codes:
            kind = PlanningIssueKind.UNSUPPORTED_CAPABILITY
            repair = RepairCandidate(RepairAction.NONE, RepairBoundary.UNSUPPORTED, target)
            requires_user = False
            changes_semantics = False
            missing = ()
        elif (
            item.code in scientific_codes
            or item.code.startswith("statistics_choice_unresolved")
            or item.code.endswith("_required")
        ):
            kind = PlanningIssueKind.SCIENTIFIC_DECISION_REQUIRED
            repair = RepairCandidate(RepairAction.ASK_USER, RepairBoundary.USER_DECISION, target)
            requires_user = True
            changes_semantics = True
            missing = (target,)
        elif any(token in item.code for token in ("_mismatch", "_changed", "_stale")) or (
            item.code.startswith("artifact_lineage_")
            or item.code.startswith("statistics_artifact_")
            or item.code.startswith("statistics_metric_")
            or item.code.startswith("statistics_mask_")
        ):
            kind = PlanningIssueKind.CONFLICTING_EVIDENCE
            repair = RepairCandidate(RepairAction.ASK_USER, RepairBoundary.USER_DECISION, target)
            requires_user = True
            changes_semantics = True
            missing = ()
        else:
            # A deterministic blocker with no registered automatic repair is
            # surfaced to the user. It is deliberately non-retryable.
            kind = PlanningIssueKind.MISSING_EVIDENCE
            repair = RepairCandidate(RepairAction.ASK_USER, RepairBoundary.USER_DECISION, target)
            requires_user = True
            changes_semantics = False
            missing = (target,)

        issue = PlanningIssue(
            code=f"statistics_preflight_{safe_code.casefold()}",
            kind=kind,
            target=target,
            message=item.message,
            missing_evidence=missing,
            evidence=(
                EvidenceRef(
                    "work_statistics_draft",
                    EvidenceSourceKind.MODEL_INFERENCE,
                    EvidenceStatus.CANDIDATE_INFERENCE,
                    draft_revision,
                ),
                *(
                    (
                        EvidenceRef(
                            target,
                            EvidenceSourceKind.UNKNOWN,
                            EvidenceStatus.UNKNOWN,
                        ),
                    )
                    if target != safe_code
                    else ()
                ),
            ),
            repairs=(repair,),
            changes_scientific_semantics=changes_semantics,
            requires_user_decision=requires_user,
            retryable=False,
        )
        results.append((issue, decide_feedback(issue, prior_fingerprints=prior)))
    return tuple(results)


def planning_feedback_for_statistics_draft_issues(
    messages: Iterable[str],
    *,
    draft_revision: str,
    prior_fingerprints: Iterable[str] = (),
) -> tuple[tuple[PlanningIssue, PreflightFeedback], ...]:
    """Treat unresolved Work statistics fields as user-controlled decisions."""

    issues: list[DesignIssue] = []
    for message in messages:
        suffix = sha256(message.encode("utf-8")).hexdigest()[:12]
        code = (
            "statistics_capability_not_validated"
            if "不受支持" in message or "尚未通过" in message
            else f"statistics_choice_unresolved_{suffix}"
        )
        issues.append(DesignIssue(code=code, message=message))
    return planning_feedback_for_design_issues(
        issues,
        draft_revision=draft_revision,
        prior_fingerprints=prior_fingerprints,
    )


def planning_feedback_for_input_conflict(
    code: str,
    message: str,
    *,
    draft_revision: str,
    manifest_revision: str,
    prior_fingerprints: Iterable[str] = (),
) -> tuple[PlanningIssue, PreflightFeedback]:
    """Fail closed when the registered project or frozen manifest moved."""
    issue = PlanningIssue(
        code=f"skill_preflight_input_conflict_{sha256(code.encode()).hexdigest()[:12]}",
        kind=PlanningIssueKind.CONFLICTING_EVIDENCE,
        target="input_manifest",
        message=message,
        evidence=(
            EvidenceRef(
                "work_draft",
                EvidenceSourceKind.MODEL_INFERENCE,
                EvidenceStatus.CANDIDATE_INFERENCE,
                draft_revision,
            ),
            EvidenceRef(
                "input_manifest",
                EvidenceSourceKind.FILE_METADATA,
                EvidenceStatus.EXECUTOR_VERIFIED,
                manifest_revision,
            ),
        ),
        repairs=(
            RepairCandidate(RepairAction.ASK_USER, RepairBoundary.USER_DECISION, "input_manifest"),
        ),
        changes_scientific_semantics=True,
        requires_user_decision=True,
        retryable=False,
    )
    return issue, decide_feedback(issue, prior_fingerprints=prior_fingerprints)


_SAFE_TARGET = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,180}$")


def _target_and_detail(message: str, namespace: str) -> tuple[str, str]:
    path, separator, detail = message.partition("：")  # noqa: RUF001
    if separator and re.fullmatch(r"[a-z][a-z0-9_.-]{0,180}", path):
        return path, detail.strip() or message
    slug = sha256(message.encode("utf-8")).hexdigest()[:12]
    return f"{namespace}.issue.{slug}", message.strip()


def _is_missing_message(message: str) -> bool:
    return any(
        token in message for token in ("尚未提供", "尚未选择", "尚未指定", "尚未给出", "未选择")
    )


def _issue_code(namespace: str, target: str, kind: str) -> str:
    suffix = sha256(f"{namespace}\0{target}".encode()).hexdigest()[:12]
    return f"{kind}_{suffix}"


def _evidence_for(
    target: str,
    fact: Any,
    sources: Mapping[str, str],
    draft_revision: str,
) -> EvidenceRef:
    if fact is not None:
        source = EvidenceSourceKind.FILE_METADATA
        status = EvidenceStatus.EXECUTOR_VERIFIED
        # Bind an observed fact to the candidate it validates. Otherwise an
        # old fingerprint can suppress feedback after the user/model changes
        # the draft while the registered fact itself remains unchanged.
        revision_value = {"draft_revision": draft_revision, "fact": fact}
    else:
        origin = sources.get(target)
        source, status = {
            "user": (EvidenceSourceKind.USER_INPUT, EvidenceStatus.USER_DECLARED),
            "data": (EvidenceSourceKind.FILE_METADATA, EvidenceStatus.EXECUTOR_VERIFIED),
            "system": (EvidenceSourceKind.EXECUTION_RECORD, EvidenceStatus.EXECUTOR_VERIFIED),
            "suggested": (EvidenceSourceKind.MODEL_INFERENCE, EvidenceStatus.CANDIDATE_INFERENCE),
        }.get(origin or "", (EvidenceSourceKind.UNKNOWN, EvidenceStatus.UNKNOWN))
        revision_value = {"draft_revision": draft_revision, "source": origin}
    revision = sha256(
        json.dumps(revision_value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    return EvidenceRef(target, source, status, revision)
