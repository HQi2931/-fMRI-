"""Application adapters from existing validator messages to typed feedback."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from hashlib import sha256
from typing import Any

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

_READABLE_METADATA_TARGETS = frozenset(
    {
        "preprocessing.tr_seconds",
        "preprocessing.expected_time_points",
        "preprocessing.slice_timing.slice_count",
        "preprocessing.slice_timing.slice_order",
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
        revision_value = fact
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
