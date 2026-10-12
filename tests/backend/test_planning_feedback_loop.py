from __future__ import annotations

import asyncio
from typing import Any

from neuroagent.agent.loop import AgentLoopBudget
from neuroagent.application.planning_feedback import (
    FeedbackDisposition,
    PlanningIssue,
    PlanningIssueKind,
    RepairAction,
    RepairBoundary,
    RepairCandidate,
)
from neuroagent.application.planning_feedback_loop import PlanningFeedbackRetrier


def _draft_issue(*, kind: PlanningIssueKind = PlanningIssueKind.INVALID_DRAFT) -> PlanningIssue:
    return PlanningIssue(
        code="invalid_bandpass",
        kind=kind,
        target="draft.bandpass",
        message="The draft needs a permitted correction.",
        repairs=(
            RepairCandidate(
                RepairAction.PATCH_UNAPPROVED_DRAFT,
                RepairBoundary.DRAFT_ONLY,
                "draft.bandpass",
            ),
        ),
        retryable=True,
    )


def _safe_budget(steps: int = 8, tokens: int = 1000, seconds: float = 30) -> AgentLoopBudget:
    return AgentLoopBudget(max_steps=steps, max_total_tokens=tokens, max_seconds=seconds)


def test_draft_feedback_retries_with_same_turn_remaining_budget() -> None:
    calls: list[tuple[dict[str, Any], AgentLoopBudget]] = []
    now = [10.0]

    async def interpret(context: dict[str, Any], budget: AgentLoopBudget):
        calls.append((context, budget))
        if len(calls) == 1:
            now[0] += 3
            return "draft-1", {"request_budget": {"model_steps": 2, "total_tokens": 200}}, None
        return "draft-2", {"request_budget": {"model_steps": 1, "total_tokens": 50}}, None

    validations = iter([_draft_issue(), None])
    retrier = PlanningFeedbackRetrier(
        interpret=interpret,
        validate=lambda _content, _metadata: next(validations),
        limits=_safe_budget(),
        clock=lambda: now[0],
    )

    result = asyncio.run(retrier.run({"question": "prepare ALFF", "work_context": {}}))

    assert result.content == "draft-2"
    assert result.disposition is FeedbackDisposition.READY
    assert result.rounds == 2
    assert calls[1][1].max_steps == 6
    assert calls[1][1].max_total_tokens == 800
    assert calls[1][1].max_seconds == 27
    assert "planning_feedback" in calls[1][0]
    assert "draft_only" in calls[1][0]["question"]
    assert result.budget.steps == 3
    assert result.budget.tokens == 250


def test_repeated_fingerprint_stops_immediately_after_one_feedback_retry() -> None:
    calls = 0
    issue = _draft_issue()

    async def interpret(_context: dict[str, Any], _budget: AgentLoopBudget):
        nonlocal calls
        calls += 1
        return f"draft-{calls}", {"request_budget": {"model_steps": 1, "total_tokens": 10}}, None

    retrier = PlanningFeedbackRetrier(
        interpret=interpret,
        validate=lambda _content, _metadata: issue,
        limits=_safe_budget(),
    )

    result = asyncio.run(retrier.run({"question": "prepare"}))

    assert calls == 2
    assert result.disposition is FeedbackDisposition.NO_PROGRESS
    assert result.rounds == 2


def test_conflict_user_decision_and_unsupported_capability_never_retry() -> None:
    cases = (
        (
            PlanningIssue(
                code="tr_conflict",
                kind=PlanningIssueKind.CONFLICTING_EVIDENCE,
                target="TR",
                message="Header and sidecar disagree.",
                repairs=(
                    RepairCandidate(
                        RepairAction.READ_REGISTERED_SOURCE, RepairBoundary.READ_ONLY, "TR"
                    ),
                ),
                retryable=True,
            ),
            FeedbackDisposition.EVIDENCE_CONFLICT,
        ),
        (
            PlanningIssue(
                code="gsr_choice",
                kind=PlanningIssueKind.SCIENTIFIC_DECISION_REQUIRED,
                target="GSR",
                message="Choose the protocol.",
                repairs=(
                    RepairCandidate(RepairAction.ASK_USER, RepairBoundary.USER_DECISION, "GSR"),
                ),
                changes_scientific_semantics=True,
                requires_user_decision=True,
            ),
            FeedbackDisposition.ASK_USER,
        ),
        (
            PlanningIssue(
                code="fc_missing",
                kind=PlanningIssueKind.UNSUPPORTED_CAPABILITY,
                target="FC",
                message="No registered FC executor.",
                repairs=(RepairCandidate(RepairAction.NONE, RepairBoundary.UNSUPPORTED, "FC"),),
            ),
            FeedbackDisposition.BLOCKED_CAPABILITY,
        ),
    )
    for issue, expected in cases:
        calls = 0

        async def interpret(_context: dict[str, Any], _budget: AgentLoopBudget):
            nonlocal calls
            calls += 1
            return "proposed", {"request_budget": {"model_steps": 1, "total_tokens": 10}}, None

        result = asyncio.run(
            PlanningFeedbackRetrier(
                interpret=interpret,
                validate=lambda _content, _metadata, current=issue: current,
                limits=_safe_budget(),
            ).run({"question": "analyze"})
        )
        assert calls == 1
        assert result.disposition is expected


def test_budget_is_not_reset_and_prevents_retry_when_steps_are_spent() -> None:
    calls = 0

    async def interpret(_context: dict[str, Any], budget: AgentLoopBudget):
        nonlocal calls
        calls += 1
        assert budget.max_steps == 2
        return "candidate", {"request_budget": {"model_steps": 2, "total_tokens": 20}}, None

    result = asyncio.run(
        PlanningFeedbackRetrier(
            interpret=interpret,
            validate=lambda _content, _metadata: _draft_issue(),
            limits=_safe_budget(steps=2),
        ).run({"question": "prepare"})
    )

    assert calls == 1
    assert result.rounds == 1
    assert result.budget.steps == 2
    assert result.budget.remaining.max_steps == 0
    assert result.disposition is FeedbackDisposition.BUDGET_EXHAUSTED


def test_zero_budget_skips_model_without_claiming_preflight_ready() -> None:
    calls = 0

    async def interpret(_context: dict[str, Any], _budget: AgentLoopBudget):
        nonlocal calls
        calls += 1
        return "unexpected", {}, None

    result = asyncio.run(
        PlanningFeedbackRetrier(
            interpret=interpret,
            validate=lambda _content, _metadata: None,
            limits=_safe_budget(steps=0),
        ).run({"question": "prepare"})
    )
    assert calls == 0
    assert result.disposition is FeedbackDisposition.BUDGET_EXHAUSTED


def test_read_evidence_candidate_is_safe_and_no_repair_stops() -> None:
    read_issue = PlanningIssue(
        code="missing_tr",
        kind=PlanningIssueKind.MISSING_EVIDENCE,
        target="TR",
        message="TR is absent from the current draft.",
        repairs=(
            RepairCandidate(
                RepairAction.READ_REGISTERED_SOURCE, RepairBoundary.READ_ONLY, "header.TR"
            ),
        ),
        retryable=True,
    )
    seen: list[dict[str, Any]] = []

    async def interpret(context: dict[str, Any], _budget: AgentLoopBudget):
        seen.append(context)
        return "candidate", {"request_budget": {"model_steps": 1, "total_tokens": 8}}, None

    result = asyncio.run(
        PlanningFeedbackRetrier(
            interpret=interpret,
            validate=lambda _content, _metadata: read_issue if len(seen) == 1 else None,
            limits=_safe_budget(),
        ).run({"question": "prepare"})
    )
    assert result.rounds == 2
    assert seen[1]["planning_feedback"]["repairs"][0]["boundary"] == "read_only"
