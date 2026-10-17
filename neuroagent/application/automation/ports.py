"""Persistence ports for durable Work task progress."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from neuroagent.application.automation.analysis_outbox import (
    AnalysisOutboxClaim,
    AnalysisOutboxStatus,
    AnalysisProposalOutbox,
)
from neuroagent.application.automation.composite_goal import (
    CompositeGoalIdentity,
    CompositeGoalSnapshot,
)
from neuroagent.application.automation.model_budget import (
    CompositeGoalModelBudget,
    CompositeGoalModelCallReserveResult,
    CompositeGoalModelCallSettlement,
)
from neuroagent.application.automation.models import (
    EventConsumptionDecision,
    TaskBudget,
    WorkEventIdentity,
    WorkTask,
    WorkTaskClaim,
)
from neuroagent.application.automation.result_interpretation import (
    ResultInterpretationClaim,
    ResultInterpretationStatus,
    StatisticalResultInterpretationOutbox,
)
from neuroagent.application.automation.step_preparation import StepPreparationClaim


class WorkTaskRepositoryPort(Protocol):
    """Store goal progress independently from the authoritative workflow state."""

    def create_task(self, task: WorkTask) -> WorkTask: ...

    def get_task(self, task_id: str) -> WorkTask: ...

    def ensure_preprocessing_composite_goal(self, task: WorkTask) -> CompositeGoalIdentity: ...

    def ensure_analysis_only_external_goal(
        self, goal: CompositeGoalIdentity, budget: TaskBudget
    ) -> CompositeGoalIdentity: ...

    def list_composite_goal_intent_ids(self, conversation_id: str) -> tuple[str, ...]: ...

    def find_composite_goal(self, intent_task_id: str) -> CompositeGoalIdentity | None: ...

    def get_composite_goal_snapshot(self, intent_task_id: str) -> CompositeGoalSnapshot: ...

    def get_composite_goal_model_budget(self, intent_task_id: str) -> CompositeGoalModelBudget: ...

    def reserve_composite_goal_model_call(
        self,
        intent_task_id: str,
        *,
        reservation_id: str,
        call_id: str,
        estimated_tokens: int,
        estimated_elapsed_seconds: int = 1,
    ) -> CompositeGoalModelCallReserveResult: ...

    def settle_composite_goal_model_call(
        self,
        intent_task_id: str,
        *,
        reservation_id: str,
        actual_tokens: int | None,
        elapsed_seconds: int,
    ) -> CompositeGoalModelCallSettlement: ...

    def find_composite_goal_for_analysis_task(
        self, analysis_work_task_id: str
    ) -> CompositeGoalIdentity | None: ...

    def bind_composite_goal_analysis_task(
        self,
        goal: CompositeGoalIdentity,
        analysis_work_task_id: str,
        proposal_hash: str,
    ) -> CompositeGoalIdentity: ...

    def save_task(
        self,
        task: WorkTask,
        *,
        expected_version: int,
        claim_token: str | None = None,
        step_preparation_claim: StepPreparationClaim | None = None,
    ) -> WorkTask: ...

    def claim_next_step_preparation(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        cooldown_seconds: float,
    ) -> StepPreparationClaim | None: ...

    def release_step_preparation_claim(self, claim: StepPreparationClaim) -> bool: ...

    def claim_next_reconciliation_task(
        self,
        worker_id: str,
        *,
        lease_seconds: int,
        cooldown_seconds: float,
    ) -> WorkTaskClaim | None: ...

    def release_reconciliation_claim(
        self, task_id: str, worker_id: str, claim_token: str
    ) -> bool: ...

    def consume_event(
        self,
        task_id: str,
        event: WorkEventIdentity,
        *,
        expected_version: int,
        updated_task: WorkTask,
        claim_token: str | None = None,
    ) -> tuple[EventConsumptionDecision, WorkTask | None]: ...

    def ensure_analysis_outbox(self, record: AnalysisProposalOutbox) -> AnalysisProposalOutbox: ...

    def get_analysis_outbox(self, intent_task_id: str) -> AnalysisProposalOutbox: ...

    def list_analysis_outboxes_for_preprocessing_task(
        self, preprocessing_task_id: str
    ) -> tuple[AnalysisProposalOutbox, ...]: ...

    def mark_analysis_outbox_ready(
        self,
        intent_task_id: str,
        *,
        preprocessing_task_id: str,
        plan_revision_id: str,
        manifest_hash: str,
        run_id: str,
        artifact_ids: tuple[str, ...],
    ) -> AnalysisProposalOutbox: ...

    def claim_analysis_outbox(
        self, worker_id: str, *, lease_seconds: int
    ) -> AnalysisOutboxClaim | None: ...

    def validate_analysis_outbox_claim(self, claim: AnalysisOutboxClaim) -> bool: ...

    def renew_analysis_outbox_claim(
        self, claim: AnalysisOutboxClaim, *, lease_seconds: int
    ) -> AnalysisOutboxClaim | None: ...

    def defer_analysis_outbox_claim(
        self,
        claim: AnalysisOutboxClaim,
        *,
        retry_after_seconds: int,
        error_code: str,
    ) -> bool: ...

    def complete_analysis_outbox(
        self,
        claim: AnalysisOutboxClaim,
        *,
        status: AnalysisOutboxStatus,
        continuation_message_id: str,
        analysis_work_task_id: str | None,
        error_code: str | None = None,
        record_attempt: bool = False,
    ) -> AnalysisProposalOutbox: ...

    def requeue_analysis_outbox(
        self, intent_task_id: str, *, recoverable_error_codes: tuple[str, ...]
    ) -> AnalysisProposalOutbox | None: ...

    def ensure_result_interpretation_outbox(
        self, record: StatisticalResultInterpretationOutbox
    ) -> StatisticalResultInterpretationOutbox: ...

    def get_result_interpretation_outbox(
        self, task_id: str
    ) -> StatisticalResultInterpretationOutbox: ...

    def claim_result_interpretation_outbox(
        self, worker_id: str, *, lease_seconds: int
    ) -> ResultInterpretationClaim | None: ...

    def renew_result_interpretation_claim(
        self, claim: ResultInterpretationClaim, *, lease_seconds: int
    ) -> ResultInterpretationClaim | None: ...

    def defer_result_interpretation_claim(
        self,
        claim: ResultInterpretationClaim,
        *,
        retry_after_seconds: int,
        error_code: str,
    ) -> bool: ...

    def complete_result_interpretation_outbox(
        self,
        claim: ResultInterpretationClaim,
        *,
        status: ResultInterpretationStatus,
        continuation_message_id: str,
        error_code: str | None = None,
        record_attempt: bool = False,
    ) -> StatisticalResultInterpretationOutbox: ...

    def requeue_result_interpretation_outbox(
        self, task_id: str, *, recoverable_error_codes: tuple[str, ...]
    ) -> StatisticalResultInterpretationOutbox | None: ...

    def atomic(self) -> AbstractContextManager[None]: ...
