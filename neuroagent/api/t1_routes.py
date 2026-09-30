"""HTTP transport for separately authorized T1 preparation and visual review."""

from typing import Annotated, cast

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import FileResponse

from neuroagent.application.contracts import RunView
from neuroagent.application.t1_preparation import (
    T1PreparationMixin,
    T1PreparationReviewCreate,
    T1PreparationReviewView,
    T1PreparationRunCreate,
)

router = APIRouter()
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


def service_from(request: Request) -> T1PreparationMixin:
    return cast(T1PreparationMixin, request.app.state.service)


@router.post(
    "/plans/{plan_revision_id}/t1-preparation-runs",
    response_model=RunView,
    status_code=status.HTTP_201_CREATED,
    tags=["t1-preparation"],
)
def create_t1_preparation_run(
    plan_revision_id: str,
    body: T1PreparationRunCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> RunView:
    return service_from(request).create_t1_preparation_run(plan_revision_id, body, idempotency_key)


@router.get(
    "/runs/{run_id}/t1-preparation-review",
    response_model=T1PreparationReviewView,
    tags=["t1-preparation"],
)
def get_t1_preparation_review(run_id: str, request: Request) -> T1PreparationReviewView:
    return service_from(request).get_t1_preparation_review(run_id)


@router.post(
    "/runs/{run_id}/t1-preparation-review",
    response_model=T1PreparationReviewView,
    status_code=status.HTTP_201_CREATED,
    tags=["t1-preparation"],
)
def review_t1_preparation(
    run_id: str, body: T1PreparationReviewCreate, request: Request, idempotency_key: IdempotencyKey
) -> T1PreparationReviewView:
    return service_from(request).review_t1_preparation(run_id, body, idempotency_key)


@router.get(
    "/runs/{run_id}/t1-preparation-artifacts/{artifact_id}/preview", tags=["t1-preparation"]
)
def t1_preparation_preview(run_id: str, artifact_id: str, request: Request) -> FileResponse:
    path, media_type = service_from(request).t1_preparation_preview_path(run_id, artifact_id)
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})
