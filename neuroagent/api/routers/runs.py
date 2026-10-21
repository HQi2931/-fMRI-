"""HTTP endpoints for the runs capability."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import StreamingResponse

from neuroagent.api.responses.agent import (
    ClusterLocalizationView,
    MlTableInspectView,
    MlTemplateView,
    RoiTableView,
    RunDiagnosisView,
)
from neuroagent.api.responses.runs import ArtifactView, QcReviewView, QcSkipView, RunView
from neuroagent.api.routers.common import IdempotencyKey, capability_from
from neuroagent.api.schemas.analysis import (
    ClusterLocalizationRequest,
    MlTableInspectRequest,
    MlTemplateCreateRequest,
    RoiTableCreateRequest,
    RunDiagnosisRequest,
)
from neuroagent.api.schemas.mapping import to_application_message, to_http_response
from neuroagent.api.schemas.runs import (
    HeadMotionQcCreate,
    QcReviewApprove,
    QcReviewCreate,
    QcSkipCreate,
    RunAction,
    RunCreate,
)
from neuroagent.application.contracts.agent import (
    ClusterLocalizationRequest as AppClusterLocalizationRequest,
)
from neuroagent.application.contracts.agent import MlTableInspectRequest as AppMlTableInspectRequest
from neuroagent.application.contracts.agent import (
    MlTemplateCreateRequest as AppMlTemplateCreateRequest,
)
from neuroagent.application.contracts.agent import RoiTableCreateRequest as AppRoiTableCreateRequest
from neuroagent.application.contracts.agent import RunDiagnosisRequest as AppRunDiagnosisRequest
from neuroagent.application.contracts.runs import HeadMotionQcCreate as AppHeadMotionQcCreate
from neuroagent.application.contracts.runs import QcReviewApprove as AppQcReviewApprove
from neuroagent.application.contracts.runs import QcReviewCreate as AppQcReviewCreate
from neuroagent.application.contracts.runs import QcSkipCreate as AppQcSkipCreate
from neuroagent.application.contracts.runs import RunAction as AppRunAction
from neuroagent.application.contracts.runs import RunCreate as AppRunCreate
from neuroagent.domain.lifecycle import WorkflowState
from neuroagent.workflow.state import TERMINAL_WORKFLOW_STATES

router = APIRouter()


@router.post(
    "/runs",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["runs"],
)
def create_run(body: RunCreate, request: Request, idempotency_key: IdempotencyKey) -> RunView:
    command = to_application_message(body, AppRunCreate)
    return cast(
        RunView,
        to_http_response(
            capability_from(request, "runs").create_run(command, idempotency_key), RunView
        ),
    )


@router.get("/runs", response_model=list[RunView], tags=["runs"])
def list_runs(
    request: Request,
    project_id: str | None = None,
    state: WorkflowState | None = None,
) -> list[RunView]:
    return cast(
        list[RunView],
        to_http_response(
            capability_from(request, "runs").list_runs(project_id=project_id, state=state),
            list[RunView],
        ),
    )


@router.get("/runs/{run_id}", response_model=RunView, tags=["runs"])
def get_run(run_id: str, request: Request) -> RunView:
    return cast(
        RunView, to_http_response(capability_from(request, "runs").get_run(run_id), RunView)
    )


@router.post(
    "/runs/{run_id}/qc-skip",
    response_model=QcSkipView,
    status_code=status.HTTP_201_CREATED,
    tags=["runs", "qc"],
)
def skip_qc(
    run_id: str,
    body: QcSkipCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> QcSkipView:
    command = to_application_message(body, AppQcSkipCreate)
    return cast(
        QcSkipView,
        to_http_response(
            capability_from(request, "runs").skip_qc(run_id, command, idempotency_key), QcSkipView
        ),
    )


@router.post("/runs/{run_id}/diagnosis", response_model=RunDiagnosisView, tags=["runs"])
def diagnose_run(
    run_id: str, body: RunDiagnosisRequest, request: Request, idempotency_key: IdempotencyKey
) -> RunDiagnosisView:
    del idempotency_key
    return cast(
        RunDiagnosisView,
        to_http_response(
            capability_from(request, "runs").diagnose_run(
                run_id, to_application_message(body, AppRunDiagnosisRequest)
            ),
            RunDiagnosisView,
        ),
    )


@router.get("/runs/{run_id}/diagnosis", response_model=RunDiagnosisView, tags=["runs"])
def diagnose_latest_run(run_id: str, request: Request) -> RunDiagnosisView:
    """Read a local diagnosis of the current attempt without uploading its logs."""

    diagnosis = capability_from(request, "runs").diagnose_latest_run(run_id)
    return cast(
        RunDiagnosisView,
        to_http_response(
            {"run_id": run_id, "diagnosis": diagnosis},
            RunDiagnosisView,
        ),
    )


@router.post("/ml/datasets/inspect", response_model=MlTableInspectView, tags=["machine-learning"])
def inspect_ml_table(
    body: MlTableInspectRequest, request: Request, idempotency_key: IdempotencyKey
) -> MlTableInspectView:
    del idempotency_key
    return cast(
        MlTableInspectView,
        to_http_response(
            capability_from(request, "runs").inspect_ml_table(
                to_application_message(body, AppMlTableInspectRequest)
            ),
            MlTableInspectView,
        ),
    )


@router.post("/ml/templates", response_model=MlTemplateView, tags=["machine-learning"])
def create_ml_template(
    body: MlTemplateCreateRequest, request: Request, idempotency_key: IdempotencyKey
) -> MlTemplateView:
    del idempotency_key
    return cast(
        MlTemplateView,
        to_http_response(
            capability_from(request, "runs").create_ml_template(
                to_application_message(body, AppMlTemplateCreateRequest)
            ),
            MlTemplateView,
        ),
    )


@router.post("/roi/extractions/validate", response_model=RoiTableView, tags=["roi"])
def validate_roi_table(
    body: RoiTableCreateRequest, request: Request, idempotency_key: IdempotencyKey
) -> RoiTableView:
    del idempotency_key
    return cast(
        RoiTableView,
        to_http_response(
            capability_from(request, "runs").validate_roi_table(
                to_application_message(body, AppRoiTableCreateRequest)
            ),
            RoiTableView,
        ),
    )


@router.post(
    "/cluster-localizations", response_model=ClusterLocalizationView, tags=["localization"]
)
def localize_clusters(
    body: ClusterLocalizationRequest, request: Request, idempotency_key: IdempotencyKey
) -> ClusterLocalizationView:
    del idempotency_key
    return cast(
        ClusterLocalizationView,
        to_http_response(
            capability_from(request, "runs").localize_clusters(
                to_application_message(body, AppClusterLocalizationRequest)
            ),
            ClusterLocalizationView,
        ),
    )


@router.post("/runs/{run_id}/cancel", response_model=RunView, tags=["runs"])
def cancel_run(
    run_id: str,
    body: RunAction,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> RunView:
    command = to_application_message(body, AppRunAction)
    return cast(
        RunView,
        to_http_response(
            capability_from(request, "runs").cancel_run(run_id, command, idempotency_key), RunView
        ),
    )


@router.post("/runs/{run_id}/retry", response_model=RunView, tags=["runs"])
def retry_run(
    run_id: str,
    body: RunAction,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> RunView:
    command = to_application_message(body, AppRunAction)
    return cast(
        RunView,
        to_http_response(
            capability_from(request, "runs").retry_run(run_id, command, idempotency_key), RunView
        ),
    )


@router.post(
    "/qc-reviews",
    response_model=QcReviewView,
    status_code=status.HTTP_201_CREATED,
    tags=["qc"],
)
def create_qc_review(
    body: QcReviewCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> QcReviewView:
    command = to_application_message(body, AppQcReviewCreate)
    return cast(
        QcReviewView,
        to_http_response(
            capability_from(request, "runs").create_qc_review(command, idempotency_key),
            QcReviewView,
        ),
    )


@router.post(
    "/runs/{run_id}/head-motion-qc",
    response_model=QcReviewView,
    status_code=status.HTTP_201_CREATED,
    tags=["qc"],
)
def create_head_motion_qc_review(
    run_id: str,
    body: HeadMotionQcCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> QcReviewView:
    return cast(
        QcReviewView,
        to_http_response(
            capability_from(request, "runs").create_head_motion_qc_review(
                run_id, to_application_message(body, AppHeadMotionQcCreate), idempotency_key
            ),
            QcReviewView,
        ),
    )


@router.get("/qc-reviews/{review_revision_id}", response_model=QcReviewView, tags=["qc"])
def get_qc_review(review_revision_id: str, request: Request) -> QcReviewView:
    return cast(
        QcReviewView,
        to_http_response(
            capability_from(request, "runs").get_qc_review(review_revision_id), QcReviewView
        ),
    )


@router.get("/runs/{run_id}/qc-review", response_model=QcReviewView | None, tags=["qc"])
def get_run_qc_review(run_id: str, request: Request) -> QcReviewView | None:
    return cast(
        QcReviewView | None,
        to_http_response(
            capability_from(request, "runs").get_run_qc_review(run_id), QcReviewView | None
        ),
    )


@router.post(
    "/qc-reviews/{review_revision_id}/approve",
    response_model=QcReviewView,
    tags=["qc"],
)
def approve_qc_review(
    review_revision_id: str,
    body: QcReviewApprove,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> QcReviewView:
    return cast(
        QcReviewView,
        to_http_response(
            capability_from(request, "runs").approve_qc_review(
                review_revision_id,
                to_application_message(body, AppQcReviewApprove),
                idempotency_key,
            ),
            QcReviewView,
        ),
    )


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactView], tags=["runs"])
def list_artifacts(run_id: str, request: Request) -> list[ArtifactView]:
    return cast(
        list[ArtifactView],
        to_http_response(
            capability_from(request, "runs").list_artifacts(run_id), list[ArtifactView]
        ),
    )


@router.get("/runs/{run_id}/events", tags=["runs"])
async def stream_run_events(
    run_id: str,
    request: Request,
    after_event_id: int = Query(default=0, ge=0),
    once: bool = Query(default=False),
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    service = capability_from(request, "runs")
    if last_event_id and last_event_id.isdigit():
        after_event_id = max(after_event_id, int(last_event_id))
    service.get_run(run_id)

    async def generate() -> AsyncIterator[str]:
        cursor = after_event_id
        while True:
            events = service.list_run_events(run_id, cursor)
            for event in events:
                cursor = event.event_id
                yield (
                    f"id: {event.event_id}\n"
                    f"event: {event.event_type}\n"
                    f"data: {event.model_dump_json()}\n\n"
                )
            current = service.get_run(run_id)
            if once or (current.state in TERMINAL_WORKFLOW_STATES and not events):
                break
            if await request.is_disconnected():
                break
            if not events:
                yield ": heartbeat\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
