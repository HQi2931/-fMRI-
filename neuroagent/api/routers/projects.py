"""HTTP endpoints for the projects capability."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Query, Request, status

from neuroagent.api.responses.projects import (
    DatasetSplitView,
    DatasetView,
    DemographicsRevisionView,
    ManifestRevisionView,
    OrganizationPreviewView,
    ProjectView,
)
from neuroagent.api.responses.runs import RuntimeEventView
from neuroagent.api.routers.common import IdempotencyKey, capability_from
from neuroagent.api.schemas.mapping import to_application_message, to_http_response
from neuroagent.api.schemas.projects import (
    DatasetCreate,
    DatasetSplitCreate,
    DemographicsImportRequest,
    ManifestScanRequest,
    OrganizationPreviewRequest,
    ProjectCreate,
)
from neuroagent.application.contracts.projects import DatasetCreate as AppDatasetCreate
from neuroagent.application.contracts.projects import DatasetSplitCreate as AppDatasetSplitCreate
from neuroagent.application.contracts.projects import (
    DemographicsImportRequest as AppDemographicsImportRequest,
)
from neuroagent.application.contracts.projects import ManifestScanRequest as AppManifestScanRequest
from neuroagent.application.contracts.projects import (
    OrganizationPreviewRequest as AppOrganizationPreviewRequest,
)
from neuroagent.application.contracts.projects import ProjectCreate as AppProjectCreate

router = APIRouter()


@router.post(
    "/projects",
    response_model=ProjectView,
    status_code=status.HTTP_201_CREATED,
    tags=["projects"],
)
def create_project(
    body: ProjectCreate, request: Request, idempotency_key: IdempotencyKey
) -> ProjectView:
    return cast(
        ProjectView,
        to_http_response(
            capability_from(request, "projects").create_project(
                to_application_message(body, AppProjectCreate), idempotency_key
            ),
            ProjectView,
        ),
    )


@router.get("/projects", response_model=list[ProjectView], tags=["projects"])
def list_projects(request: Request) -> list[ProjectView]:
    return cast(
        list[ProjectView],
        to_http_response(capability_from(request, "projects").list_projects(), list[ProjectView]),
    )


@router.get("/projects/{project_id}", response_model=ProjectView, tags=["projects"])
def get_project(project_id: str, request: Request) -> ProjectView:
    return cast(
        ProjectView,
        to_http_response(capability_from(request, "projects").get_project(project_id), ProjectView),
    )


@router.get(
    "/projects/{project_id}/audit-events",
    response_model=list[RuntimeEventView],
    tags=["projects"],
)
def project_audit_events(
    project_id: str, request: Request, after_event_id: int = Query(default=0, ge=0)
) -> list[RuntimeEventView]:
    return cast(
        list[RuntimeEventView],
        to_http_response(
            capability_from(request, "runs").list_project_events(project_id, after_event_id),
            list[RuntimeEventView],
        ),
    )


@router.post(
    "/projects/{project_id}/datasets",
    response_model=DatasetView,
    status_code=status.HTTP_201_CREATED,
    tags=["datasets"],
)
def create_dataset(
    project_id: str,
    body: DatasetCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> DatasetView:
    return cast(
        DatasetView,
        to_http_response(
            capability_from(request, "projects").create_dataset(
                project_id, to_application_message(body, AppDatasetCreate), idempotency_key
            ),
            DatasetView,
        ),
    )


@router.get("/datasets/{dataset_id}", response_model=DatasetView, tags=["datasets"])
def get_dataset(dataset_id: str, request: Request) -> DatasetView:
    return cast(
        DatasetView,
        to_http_response(capability_from(request, "projects").get_dataset(dataset_id), DatasetView),
    )


@router.post(
    "/datasets/{dataset_id}/inspect",
    response_model=ManifestRevisionView,
    status_code=status.HTTP_201_CREATED,
    tags=["datasets"],
)
def inspect_dataset(
    dataset_id: str,
    body: ManifestScanRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ManifestRevisionView:
    return cast(
        ManifestRevisionView,
        to_http_response(
            capability_from(request, "projects").inspect_dataset(
                dataset_id, to_application_message(body, AppManifestScanRequest), idempotency_key
            ),
            ManifestRevisionView,
        ),
    )


@router.get("/manifests/{manifest_id}", response_model=ManifestRevisionView, tags=["datasets"])
def get_manifest(manifest_id: str, request: Request) -> ManifestRevisionView:
    return cast(
        ManifestRevisionView,
        to_http_response(
            capability_from(request, "projects").get_manifest(manifest_id), ManifestRevisionView
        ),
    )


@router.post(
    "/datasets/{dataset_id}/demographics/import",
    response_model=DemographicsRevisionView,
    status_code=status.HTTP_201_CREATED,
    tags=["datasets"],
)
def import_demographics(
    dataset_id: str,
    body: DemographicsImportRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> DemographicsRevisionView:
    return cast(
        DemographicsRevisionView,
        to_http_response(
            capability_from(request, "projects").import_demographics(
                dataset_id,
                to_application_message(body, AppDemographicsImportRequest),
                idempotency_key,
            ),
            DemographicsRevisionView,
        ),
    )


@router.get(
    "/demographics/{demographics_id}",
    response_model=DemographicsRevisionView,
    tags=["datasets"],
)
def get_demographics(demographics_id: str, request: Request) -> DemographicsRevisionView:
    return cast(
        DemographicsRevisionView,
        to_http_response(
            capability_from(request, "projects").get_demographics(demographics_id),
            DemographicsRevisionView,
        ),
    )


@router.post(
    "/datasets/{dataset_id}/splits",
    response_model=DatasetSplitView,
    status_code=status.HTTP_201_CREATED,
    tags=["datasets"],
)
def create_split(
    dataset_id: str,
    body: DatasetSplitCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> DatasetSplitView:
    return cast(
        DatasetSplitView,
        to_http_response(
            capability_from(request, "projects").create_split(
                dataset_id, to_application_message(body, AppDatasetSplitCreate), idempotency_key
            ),
            DatasetSplitView,
        ),
    )


@router.get("/splits/{split_id}", response_model=DatasetSplitView, tags=["datasets"])
def get_split(split_id: str, request: Request) -> DatasetSplitView:
    return cast(
        DatasetSplitView,
        to_http_response(
            capability_from(request, "projects").get_split(split_id), DatasetSplitView
        ),
    )


@router.post("/organization/previews", response_model=OrganizationPreviewView, tags=["datasets"])
def organization_preview(
    body: OrganizationPreviewRequest, request: Request, idempotency_key: IdempotencyKey
) -> OrganizationPreviewView:
    del idempotency_key
    return cast(
        OrganizationPreviewView,
        to_http_response(
            capability_from(request, "runs").organization_preview(
                to_application_message(body, AppOrganizationPreviewRequest)
            ),
            OrganizationPreviewView,
        ),
    )
