"""Immutable plan revision and approval use cases."""

from __future__ import annotations

from neuroagent.application.contracts import (
    ApprovalCreate,
    ApprovalView,
    PlanRevisionCreate,
    PlanRevisionView,
    PlanValidationRequest,
)
from neuroagent.application.hashing import content_hash
from neuroagent.application.service_mixins._base import BaseServiceMixin


class PlanApprovalMixin(BaseServiceMixin):
    def create_plan(self, request: PlanRevisionCreate, idempotency_key: str) -> PlanRevisionView:
        def action() -> PlanRevisionView:
            lock = {
                "plan": request.plan,
                "manifest_hash": request.manifest_hash,
                "environment_hash": request.environment_hash,
            }
            result = self.repository.create_plan(
                project_id=request.project_id,
                expected_project_version=request.expected_project_version,
                plan=request.plan,
                plan_hash=content_hash(lock),
                manifest_hash=request.manifest_hash,
                environment_hash=request.environment_hash,
                supersedes_id=request.supersedes_plan_revision_id,
            )
            self.repository.append_event(
                project_id=result.project_id,
                run_id=None,
                event_type="PlanRevisionCreated",
                severity="info",
                payload={
                    "plan_revision_id": result.plan_revision_id,
                    "plan_hash": result.plan_hash,
                    "revision": result.revision,
                },
            )
            return result

        return self._idempotent(
            scope=f"projects:{request.project_id}:plans:create",
            key=idempotency_key,
            request=request,
            response_type=PlanRevisionView,
            action=action,
        )

    def get_plan(self, plan_revision_id: str) -> PlanRevisionView:
        return self.repository.get_plan(plan_revision_id)

    def validate_plan(
        self,
        plan_revision_id: str,
        request: PlanValidationRequest,
        idempotency_key: str,
    ) -> PlanRevisionView:
        def action() -> PlanRevisionView:
            result = self.repository.validate_plan(
                plan_revision_id,
                expected_version=request.expected_version,
                issues=request.issues,
            )
            self.repository.append_event(
                project_id=result.project_id,
                run_id=None,
                event_type="PlanValidated",
                severity=(
                    "error"
                    if any(issue.severity == "blocking" for issue in request.issues)
                    else "info"
                ),
                payload={
                    "plan_revision_id": plan_revision_id,
                    "plan_hash": result.plan_hash,
                    "state": result.state.value,
                    "issue_count": len(request.issues),
                },
            )
            return result

        return self._idempotent(
            scope=f"plans:{plan_revision_id}:validate",
            key=idempotency_key,
            request=request,
            response_type=PlanRevisionView,
            action=action,
        )

    def approve_plan(
        self, plan_revision_id: str, request: ApprovalCreate, idempotency_key: str
    ) -> ApprovalView:
        def action() -> ApprovalView:
            approval, plan = self.repository.record_approval(plan_revision_id, request)
            self.repository.append_event(
                project_id=plan.project_id,
                run_id=None,
                event_type="PlanApprovalRecorded",
                severity="info" if request.decision.value == "approved" else "warning",
                payload={
                    "plan_revision_id": plan_revision_id,
                    "approval_id": approval.approval_id,
                    "decision": approval.decision.value,
                    "plan_hash": approval.plan_hash,
                },
            )
            return approval

        return self._idempotent(
            scope=f"plans:{plan_revision_id}:approve",
            key=idempotency_key,
            request=request,
            response_type=ApprovalView,
            action=action,
        )
