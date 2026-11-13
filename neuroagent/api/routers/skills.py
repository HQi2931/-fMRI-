"""HTTP endpoints for the skills capability."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, status

from neuroagent.api.responses.plans import SkillPlanDryRunView, SkillPlanResolveView
from neuroagent.api.routers.common import IdempotencyKey, capability_from
from neuroagent.api.schemas.mapping import to_application_message, to_http_response
from neuroagent.api.schemas.plans import SkillPlanResolveRequest
from neuroagent.application.contracts.plans import (
    SkillPlanResolveRequest as AppSkillPlanResolveRequest,
)
from neuroagent.skills.models import SkillSpec

router = APIRouter()


@router.get("/skills", response_model=list[SkillSpec], tags=["skills"])
def list_skills(request: Request) -> list[SkillSpec]:
    return capability_from(request, "skills").list_skills()


@router.post(
    "/skill-plans/resolve",
    response_model=SkillPlanResolveView,
    status_code=status.HTTP_201_CREATED,
    tags=["skills"],
)
def resolve_skill_plan(
    body: SkillPlanResolveRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> SkillPlanResolveView:
    return cast(
        SkillPlanResolveView,
        to_http_response(
            capability_from(request, "skills").resolve_skill_plan(
                to_application_message(body, AppSkillPlanResolveRequest), idempotency_key
            ),
            SkillPlanResolveView,
        ),
    )


@router.post(
    "/skill-plans/dry-run",
    response_model=SkillPlanDryRunView,
    status_code=status.HTTP_200_OK,
    tags=["skills"],
)
def dry_run_skill_plan(
    body: SkillPlanResolveRequest,
    request: Request,
) -> SkillPlanDryRunView:
    return cast(
        SkillPlanDryRunView,
        to_http_response(
            capability_from(request, "skills").dry_run_skill_plan(
                to_application_message(body, AppSkillPlanResolveRequest)
            ),
            SkillPlanDryRunView,
        ),
    )
