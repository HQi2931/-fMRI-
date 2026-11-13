"""HTTP endpoints for the plans capability."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, status

from neuroagent.api.responses.plans import ApprovalView, PlanRevisionView
from neuroagent.api.routers.common import IdempotencyKey, capability_from
from neuroagent.api.schemas.mapping import to_application_message, to_http_response
from neuroagent.api.schemas.plans import ApprovalCreate
from neuroagent.application.contracts.plans import ApprovalCreate as AppApprovalCreate

router = APIRouter()


@router.get(
    "/plan-revisions/{plan_revision_id}",
    response_model=PlanRevisionView,
    tags=["plans"],
)
def get_plan(plan_revision_id: str, request: Request) -> PlanRevisionView:
    return cast(
        PlanRevisionView,
        to_http_response(
            capability_from(request, "plans").get_plan(plan_revision_id), PlanRevisionView
        ),
    )


@router.post(
    "/plan-revisions/{plan_revision_id}/approve",
    response_model=ApprovalView,
    status_code=status.HTTP_201_CREATED,
    tags=["plans"],
)
def approve_plan(
    plan_revision_id: str,
    body: ApprovalCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ApprovalView:
    return cast(
        ApprovalView,
        to_http_response(
            capability_from(request, "plans").approve_plan(
                plan_revision_id, to_application_message(body, AppApprovalCreate), idempotency_key
            ),
            ApprovalView,
        ),
    )
