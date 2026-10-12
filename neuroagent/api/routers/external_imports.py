"""HTTP endpoints for the external imports capability."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Query, Request, status

from neuroagent.api.responses.external_imports import (
    ExternalAnalysisImportSummaryView,
    ExternalAnalysisImportView,
)
from neuroagent.api.responses.projects import ExternalDemographicsRevisionView
from neuroagent.api.routers.common import IdempotencyKey, capability_from
from neuroagent.api.schemas.external_imports import (
    ExternalAnalysisImportIncludeRequest,
    ExternalAnalysisImportInspectRequest,
)
from neuroagent.api.schemas.mapping import to_application_message, to_http_response
from neuroagent.api.schemas.projects import ExternalDemographicsImportRequest
from neuroagent.application.contracts.external_imports import (
    ExternalAnalysisImportIncludeRequest as AppExternalAnalysisImportIncludeRequest,
)
from neuroagent.application.contracts.external_imports import (
    ExternalAnalysisImportInspectRequest as AppExternalAnalysisImportInspectRequest,
)
from neuroagent.application.contracts.projects import (
    ExternalDemographicsImportRequest as AppExternalDemographicsImportRequest,
)

router = APIRouter()


@router.post(
    "/statistics/external-imports/inspect",
    response_model=ExternalAnalysisImportView,
    status_code=status.HTTP_201_CREATED,
    tags=["statistics", "external-data"],
)
def inspect_external_analysis_import(
    body: ExternalAnalysisImportInspectRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ExternalAnalysisImportView:
    return cast(
        ExternalAnalysisImportView,
        to_http_response(
            capability_from(request, "external_imports").inspect_external_analysis_import(
                to_application_message(body, AppExternalAnalysisImportInspectRequest),
                idempotency_key,
            ),
            ExternalAnalysisImportView,
        ),
    )


@router.get(
    "/statistics/external-imports",
    response_model=list[ExternalAnalysisImportSummaryView],
    tags=["statistics", "external-data"],
)
def list_external_analysis_imports(
    request: Request,
    project_id: str = Query(...),
) -> list[ExternalAnalysisImportSummaryView]:
    return cast(
        list[ExternalAnalysisImportSummaryView],
        to_http_response(
            capability_from(request, "external_imports").list_external_analysis_imports(
                project_id=project_id
            ),
            list[ExternalAnalysisImportSummaryView],
        ),
    )


@router.get(
    "/statistics/external-imports/{import_id}",
    response_model=ExternalAnalysisImportView,
    tags=["statistics", "external-data"],
)
def get_external_analysis_import(import_id: str, request: Request) -> ExternalAnalysisImportView:
    return cast(
        ExternalAnalysisImportView,
        to_http_response(
            capability_from(request, "external_imports").get_external_analysis_import(import_id),
            ExternalAnalysisImportView,
        ),
    )


@router.post(
    "/statistics/external-imports/{import_id}/include",
    response_model=ExternalAnalysisImportView,
    tags=["statistics", "external-data"],
)
def include_external_analysis_import(
    import_id: str,
    body: ExternalAnalysisImportIncludeRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ExternalAnalysisImportView:
    return cast(
        ExternalAnalysisImportView,
        to_http_response(
            capability_from(request, "external_imports").include_external_analysis_import(
                import_id,
                to_application_message(body, AppExternalAnalysisImportIncludeRequest),
                idempotency_key,
            ),
            ExternalAnalysisImportView,
        ),
    )


@router.post(
    "/statistics/external-imports/{import_id}/demographics",
    response_model=ExternalDemographicsRevisionView,
    status_code=status.HTTP_201_CREATED,
    tags=["statistics", "external-data"],
)
def import_external_demographics(
    import_id: str,
    body: ExternalDemographicsImportRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ExternalDemographicsRevisionView:
    return cast(
        ExternalDemographicsRevisionView,
        to_http_response(
            capability_from(request, "external_imports").import_external_demographics(
                import_id,
                to_application_message(body, AppExternalDemographicsImportRequest),
                idempotency_key,
            ),
            ExternalDemographicsRevisionView,
        ),
    )


@router.get(
    "/statistics/external-imports/{import_id}/demographics",
    response_model=ExternalDemographicsRevisionView | None,
    tags=["statistics", "external-data"],
)
def get_external_demographics(
    import_id: str, request: Request
) -> ExternalDemographicsRevisionView | None:
    return cast(
        ExternalDemographicsRevisionView | None,
        to_http_response(
            capability_from(request, "external_imports").get_external_demographics(import_id),
            ExternalDemographicsRevisionView | None,
        ),
    )
