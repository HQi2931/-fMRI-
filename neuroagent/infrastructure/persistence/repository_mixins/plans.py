"""Immutable plan revisions and approvals."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from neuroagent.application.contracts import (
    ApprovalCreate,
    ApprovalDecision,
    ApprovalView,
    PlanRevisionView,
    PlanState,
    ValidationIssue,
)
from neuroagent.application.errors import ConflictError, NotFoundError
from neuroagent.application.hashing import canonical_json
from neuroagent.infrastructure.persistence.models import ApprovalRow, PlanRevisionRow, ProjectRow
from neuroagent.infrastructure.persistence.repository_mixins._base import (
    RepositoryBaseMixin,
    _id,
)
from neuroagent.workflow.state import validate_plan_transition


class PlanApprovalMixin(RepositoryBaseMixin):
    # -- immutable plans and approvals --------------------------------------

    def create_plan(
        self,
        *,
        project_id: str,
        expected_project_version: int,
        plan: dict[str, Any],
        plan_hash: str,
        manifest_hash: str,
        environment_hash: str,
        supersedes_id: str | None,
    ) -> PlanRevisionView:
        with self._write_session() as session:
            project = session.get(ProjectRow, project_id)
            if project is None:
                raise NotFoundError("project", project_id)
            self._check_version("project", project.version, expected_project_version)
            revision = (
                session.scalar(
                    select(func.max(PlanRevisionRow.revision)).where(
                        PlanRevisionRow.project_id == project_id
                    )
                )
                or 0
            ) + 1
            if supersedes_id:
                old = session.get(PlanRevisionRow, supersedes_id)
                if old is None:
                    raise NotFoundError("plan_revision", supersedes_id)
                if old.project_id != project_id:
                    raise ConflictError(
                        "cross_project_supersede",
                        "不能替代其他项目的计划。",
                    )
                old_state = PlanState(old.state)
                validate_plan_transition(old_state, PlanState.SUPERSEDED)
                old.state = PlanState.SUPERSEDED.value
                old.version += 1
                old.updated_at = datetime.now(UTC)
            row = PlanRevisionRow(
                plan_revision_id=_id(),
                project_id=project_id,
                revision=revision,
                state=PlanState.DRAFT.value,
                plan_hash=plan_hash,
                manifest_hash=manifest_hash,
                environment_hash=environment_hash,
                plan_json=canonical_json(plan),
                validation_issues_json="[]",
                supersedes_plan_revision_id=supersedes_id,
            )
            session.add(row)
            session.flush()
            return self._plan(row)

    def get_plan(self, plan_revision_id: str) -> PlanRevisionView:
        with self.database.session_factory() as session:
            row = session.get(PlanRevisionRow, plan_revision_id)
            if row is None:
                raise NotFoundError("plan_revision", plan_revision_id)
            return self._plan(row)

    def validate_plan(
        self,
        plan_revision_id: str,
        *,
        expected_version: int,
        issues: list[ValidationIssue],
    ) -> PlanRevisionView:
        with self._write_session() as session:
            row = session.get(PlanRevisionRow, plan_revision_id)
            if row is None:
                raise NotFoundError("plan_revision", plan_revision_id)
            self._check_version("plan_revision", row.version, expected_version)
            current = PlanState(row.state)
            validate_plan_transition(current, PlanState.VALIDATING)
            target = (
                PlanState.DRAFT
                if any(issue.severity == "blocking" for issue in issues)
                else PlanState.AWAITING_APPROVAL
            )
            validate_plan_transition(PlanState.VALIDATING, target)
            row.state = target.value
            row.validation_issues_json = canonical_json(
                [issue.model_dump(mode="json") for issue in issues]
            )
            row.version += 1
            row.updated_at = datetime.now(UTC)
            session.flush()
            return self._plan(row)

    def record_approval(
        self, plan_revision_id: str, request: ApprovalCreate
    ) -> tuple[ApprovalView, PlanRevisionView]:
        with self._write_session() as session:
            plan = session.get(PlanRevisionRow, plan_revision_id)
            if plan is None:
                raise NotFoundError("plan_revision", plan_revision_id)
            self._check_version("plan_revision", plan.version, request.expected_version)
            if PlanState(plan.state) is not PlanState.AWAITING_APPROVAL:
                raise ConflictError(
                    "plan_not_awaiting_approval",
                    "计划当前不处于待审批状态。",
                    state=plan.state,
                )
            if plan.plan_hash != request.plan_hash:
                raise ConflictError(
                    "approval_hash_mismatch",
                    "审批哈希与当前不可变计划不一致。",
                    expected=plan.plan_hash,
                    received=request.plan_hash,
                )
            target = (
                PlanState.APPROVED
                if request.decision is ApprovalDecision.APPROVED
                else PlanState.DRAFT
            )
            validate_plan_transition(PlanState.AWAITING_APPROVAL, target)
            approval = ApprovalRow(
                approval_id=_id(),
                plan_revision_id=plan_revision_id,
                plan_hash=request.plan_hash,
                actor=request.actor,
                decision=request.decision.value,
                reason=request.reason,
            )
            session.add(approval)
            plan.state = target.value
            plan.version += 1
            plan.updated_at = datetime.now(UTC)
            session.flush()
            return self._approval(approval), self._plan(plan)

    def has_valid_approval(self, plan_revision_id: str, plan_hash: str) -> bool:
        with self.database.session_factory() as session:
            return (
                session.scalar(
                    select(func.count(ApprovalRow.approval_id)).where(
                        ApprovalRow.plan_revision_id == plan_revision_id,
                        ApprovalRow.plan_hash == plan_hash,
                        ApprovalRow.decision == ApprovalDecision.APPROVED.value,
                    )
                )
                or 0
            ) > 0

    def get_approved_plan_approval(self, plan_revision_id: str) -> ApprovalView:
        with self.database.session_factory() as session:
            row = session.scalar(
                select(ApprovalRow)
                .where(
                    ApprovalRow.plan_revision_id == plan_revision_id,
                    ApprovalRow.decision == ApprovalDecision.APPROVED.value,
                )
                .order_by(ApprovalRow.created_at.desc(), ApprovalRow.approval_id)
            )
            if row is None:
                raise ConflictError("approval_missing", "未找到已批准的计划审批记录。")
            return self._approval(row)
