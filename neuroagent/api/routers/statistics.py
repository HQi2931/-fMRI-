"""HTTP endpoints for the statistics capability."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import Response

from neuroagent.api.responses.plans import ApprovalView
from neuroagent.api.responses.runs import RunView
from neuroagent.api.responses.statistics import (
    CorrectionCapabilityView,
    StatisticalAtlasView,
    StatisticalClusterRoiView,
    StatisticalDesignView,
    StatisticalResultDetailView,
    StatisticalResultLocalizationView,
    StatisticalResultSliceInfoView,
    StatisticalResultView,
)
from neuroagent.api.routers.common import IdempotencyKey, capability_from
from neuroagent.api.schemas.mapping import to_application_message, to_http_response
from neuroagent.api.schemas.plans import (
    ApprovalCreate,
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticsRunCreate,
)
from neuroagent.application.contracts.plans import ApprovalCreate as AppApprovalCreate
from neuroagent.application.contracts.statistics import (
    StatisticalDesignCreate as AppStatisticalDesignCreate,
)
from neuroagent.application.contracts.statistics import (
    StatisticalDesignValidationRequest as AppStatisticalDesignValidationRequest,
)
from neuroagent.application.contracts.statistics import (
    StatisticsRunCreate as AppStatisticsRunCreate,
)

router = APIRouter()


@router.post(
    "/statistical-designs",
    response_model=StatisticalDesignView,
    status_code=status.HTTP_201_CREATED,
    tags=["statistics"],
)
def create_statistical_design(
    body: StatisticalDesignCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> StatisticalDesignView:
    return cast(
        StatisticalDesignView,
        to_http_response(
            capability_from(request, "statistics").create_statistical_design(
                to_application_message(body, AppStatisticalDesignCreate), idempotency_key
            ),
            StatisticalDesignView,
        ),
    )


@router.get(
    "/statistical-designs/{plan_revision_id}",
    response_model=StatisticalDesignView,
    tags=["statistics"],
)
def get_statistical_design(plan_revision_id: str, request: Request) -> StatisticalDesignView:
    return cast(
        StatisticalDesignView,
        to_http_response(
            capability_from(request, "statistics").get_statistical_design(plan_revision_id),
            StatisticalDesignView,
        ),
    )


@router.post(
    "/statistical-designs/{plan_revision_id}/validate",
    response_model=StatisticalDesignView,
    tags=["statistics"],
)
def validate_statistical_design(
    plan_revision_id: str,
    body: StatisticalDesignValidationRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> StatisticalDesignView:
    return cast(
        StatisticalDesignView,
        to_http_response(
            capability_from(request, "statistics").validate_statistical_design(
                plan_revision_id,
                to_application_message(body, AppStatisticalDesignValidationRequest),
                idempotency_key,
            ),
            StatisticalDesignView,
        ),
    )


@router.post(
    "/statistical-designs/{plan_revision_id}/approve",
    response_model=ApprovalView,
    status_code=status.HTTP_201_CREATED,
    tags=["statistics"],
)
def approve_statistical_design(
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


@router.get(
    "/corrections",
    response_model=list[CorrectionCapabilityView],
    tags=["statistics"],
)
def list_correction_capabilities(request: Request) -> list[CorrectionCapabilityView]:
    return cast(
        list[CorrectionCapabilityView],
        to_http_response(
            capability_from(request, "statistics").correction_capabilities(),
            list[CorrectionCapabilityView],
        ),
    )


@router.post(
    "/statistics/runs",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["statistics"],
)
def create_statistics_run(
    body: StatisticsRunCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> RunView:
    return cast(
        RunView,
        to_http_response(
            capability_from(request, "runs").create_statistics_run(
                to_application_message(body, AppStatisticsRunCreate), idempotency_key
            ),
            RunView,
        ),
    )


@router.get(
    "/statistics/results",
    response_model=list[StatisticalResultView],
    tags=["statistics"],
)
def list_statistical_results(
    request: Request,
    project_id: str = Query(...),
    run_id: str | None = Query(default=None),
) -> list[StatisticalResultView]:
    return cast(
        list[StatisticalResultView],
        to_http_response(
            capability_from(request, "runs").list_statistical_results(
                project_id=project_id, run_id=run_id
            ),
            list[StatisticalResultView],
        ),
    )


@router.get(
    "/statistics/results/{result_id}",
    response_model=StatisticalResultDetailView,
    tags=["statistics"],
)
def get_statistical_result(result_id: str, request: Request) -> StatisticalResultDetailView:
    return cast(
        StatisticalResultDetailView,
        to_http_response(
            capability_from(request, "runs").get_statistical_result(result_id),
            StatisticalResultDetailView,
        ),
    )


@router.get(
    "/statistics/atlases",
    response_model=tuple[StatisticalAtlasView, ...],
    tags=["statistics", "localization"],
)
def list_statistical_atlases(request: Request) -> tuple[StatisticalAtlasView, ...]:
    return cast(
        tuple[StatisticalAtlasView, ...],
        to_http_response(
            capability_from(request, "runs").list_statistical_atlases(),
            tuple[StatisticalAtlasView, ...],
        ),
    )


@router.get(
    "/statistics/results/{result_id}/localization",
    response_model=StatisticalResultLocalizationView,
    tags=["statistics", "localization"],
)
def localize_statistical_result(
    result_id: str,
    request: Request,
    atlas_id: str = Query(..., min_length=1, max_length=40),
) -> StatisticalResultLocalizationView:
    return cast(
        StatisticalResultLocalizationView,
        to_http_response(
            capability_from(request, "runs").localize_statistical_result(result_id, atlas_id),
            StatisticalResultLocalizationView,
        ),
    )


@router.get(
    "/statistics/results/{result_id}/slice-info",
    response_model=StatisticalResultSliceInfoView,
    tags=["statistics", "preview"],
)
def statistical_result_slice_info(
    result_id: str, request: Request
) -> StatisticalResultSliceInfoView:
    return cast(
        StatisticalResultSliceInfoView,
        to_http_response(
            capability_from(request, "runs").statistical_result_slice_info(result_id),
            StatisticalResultSliceInfoView,
        ),
    )


@router.get(
    "/statistics/results/{result_id}/roi",
    response_model=StatisticalClusterRoiView,
    tags=["statistics", "roi"],
)
def statistical_cluster_roi(
    result_id: str, request: Request, cluster_id: str | None = Query(default=None, max_length=100)
) -> StatisticalClusterRoiView:
    return cast(
        StatisticalClusterRoiView,
        to_http_response(
            capability_from(request, "runs").statistical_cluster_roi(result_id, cluster_id),
            StatisticalClusterRoiView,
        ),
    )


@router.get(
    "/statistics/results/{result_id}/slice.png",
    response_class=Response,
    tags=["statistics", "preview"],
)
def statistical_result_slice_preview(
    result_id: str,
    request: Request,
    slice_index: int = Query(..., ge=0, le=511),
) -> Response:
    return Response(
        capability_from(request, "runs").statistical_result_slice_png(result_id, slice_index),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )
