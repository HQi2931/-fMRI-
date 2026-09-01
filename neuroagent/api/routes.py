"""REST and SSE routes; no scientific rules or process execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import APIRouter, Header, Query, Request, status
from fastapi.responses import StreamingResponse

from neuroagent.application.contracts import (
    AgentTaskCreate,
    AgentTaskView,
    ApprovalCreate,
    ApprovalView,
    ArtifactView,
    ClusterLocalizationRequest,
    ClusterLocalizationView,
    CorrectionCapabilityView,
    DatasetCreate,
    DatasetSplitCreate,
    DatasetSplitView,
    DatasetView,
    DemographicsImportRequest,
    DemographicsRevisionView,
    EnvironmentProbeView,
    HealthView,
    ManifestRevisionView,
    ManifestScanRequest,
    MlTableInspectRequest,
    MlTableInspectView,
    MlTemplateCreateRequest,
    MlTemplateView,
    ModelListRequest,
    ModelListView,
    ModelProfileCreate,
    ModelProfileView,
    OrganizationPreviewRequest,
    OrganizationPreviewView,
    PlanRevisionView,
    ProjectCreate,
    ProjectView,
    ProviderTestRequest,
    ProviderTestView,
    QcReviewApprove,
    QcReviewCreate,
    QcReviewView,
    RoiTableCreateRequest,
    RoiTableView,
    RsFmriAnswerView,
    RsFmriQuestionRequest,
    RunAction,
    RunCreate,
    RunDiagnosisRequest,
    RunDiagnosisView,
    RuntimeEventView,
    RunView,
    SkillPlanResolveRequest,
    SkillPlanResolveView,
    StatisticalDesignCreate,
    StatisticalDesignValidationRequest,
    StatisticalDesignView,
    StatisticalResultDetailView,
    StatisticalResultView,
    StatisticsRunCreate,
    WorkflowState,
)
from neuroagent.application.services import NeuroAgentService
from neuroagent.skills.models import SkillSpec
from neuroagent.workflow.state import TERMINAL_WORKFLOW_STATES

router = APIRouter()
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


def service_from(request: Request) -> NeuroAgentService:
    return cast(NeuroAgentService, request.app.state.service)


@router.get("/health", response_model=HealthView, tags=["system"])
def health(request: Request) -> HealthView:
    return service_from(request).health()


@router.get("/environment/probe", response_model=EnvironmentProbeView, tags=["system"])
def environment_probe(request: Request) -> EnvironmentProbeView:
    return service_from(request).environment_probe()


@router.post(
    "/projects",
    response_model=ProjectView,
    status_code=status.HTTP_201_CREATED,
    tags=["projects"],
)
def create_project(
    body: ProjectCreate, request: Request, idempotency_key: IdempotencyKey
) -> ProjectView:
    return service_from(request).create_project(body, idempotency_key)


@router.get("/projects", response_model=list[ProjectView], tags=["projects"])
def list_projects(request: Request) -> list[ProjectView]:
    return service_from(request).list_projects()


@router.get("/projects/{project_id}", response_model=ProjectView, tags=["projects"])
def get_project(project_id: str, request: Request) -> ProjectView:
    return service_from(request).get_project(project_id)


@router.get(
    "/projects/{project_id}/audit-events",
    response_model=list[RuntimeEventView],
    tags=["projects"],
)
def project_audit_events(
    project_id: str, request: Request, after_event_id: int = Query(default=0, ge=0)
) -> list[RuntimeEventView]:
    return service_from(request).list_project_events(project_id, after_event_id)


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
    return service_from(request).create_dataset(project_id, body, idempotency_key)


@router.get("/datasets/{dataset_id}", response_model=DatasetView, tags=["datasets"])
def get_dataset(dataset_id: str, request: Request) -> DatasetView:
    return service_from(request).get_dataset(dataset_id)


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
    return service_from(request).inspect_dataset(dataset_id, body, idempotency_key)


@router.get("/manifests/{manifest_id}", response_model=ManifestRevisionView, tags=["datasets"])
def get_manifest(manifest_id: str, request: Request) -> ManifestRevisionView:
    return service_from(request).get_manifest(manifest_id)


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
    return service_from(request).import_demographics(dataset_id, body, idempotency_key)


@router.get(
    "/demographics/{demographics_id}",
    response_model=DemographicsRevisionView,
    tags=["datasets"],
)
def get_demographics(demographics_id: str, request: Request) -> DemographicsRevisionView:
    return service_from(request).get_demographics(demographics_id)


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
    return service_from(request).create_split(dataset_id, body, idempotency_key)


@router.get("/splits/{split_id}", response_model=DatasetSplitView, tags=["datasets"])
def get_split(split_id: str, request: Request) -> DatasetSplitView:
    return service_from(request).get_split(split_id)


@router.get(
    "/plan-revisions/{plan_revision_id}",
    response_model=PlanRevisionView,
    tags=["plans"],
)
def get_plan(plan_revision_id: str, request: Request) -> PlanRevisionView:
    return service_from(request).get_plan(plan_revision_id)


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
    return service_from(request).approve_plan(plan_revision_id, body, idempotency_key)


@router.get("/skills", response_model=list[SkillSpec], tags=["skills"])
def list_skills(request: Request) -> list[SkillSpec]:
    return service_from(request).list_skills()


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
    return service_from(request).resolve_skill_plan(body, idempotency_key)


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
    return service_from(request).create_statistical_design(body, idempotency_key)


@router.get(
    "/statistical-designs/{plan_revision_id}",
    response_model=StatisticalDesignView,
    tags=["statistics"],
)
def get_statistical_design(plan_revision_id: str, request: Request) -> StatisticalDesignView:
    return service_from(request).get_statistical_design(plan_revision_id)


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
    return service_from(request).validate_statistical_design(
        plan_revision_id, body, idempotency_key
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
    return service_from(request).approve_plan(plan_revision_id, body, idempotency_key)


@router.get(
    "/corrections",
    response_model=list[CorrectionCapabilityView],
    tags=["statistics"],
)
def list_correction_capabilities() -> list[CorrectionCapabilityView]:
    return NeuroAgentService.correction_capabilities()


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
    return service_from(request).create_statistics_run(body, idempotency_key)


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
    return service_from(request).list_statistical_results(project_id=project_id, run_id=run_id)


@router.get(
    "/statistics/results/{result_id}",
    response_model=StatisticalResultDetailView,
    tags=["statistics"],
)
def get_statistical_result(result_id: str, request: Request) -> StatisticalResultDetailView:
    return service_from(request).get_statistical_result(result_id)


@router.post(
    "/runs",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["runs"],
)
def create_run(body: RunCreate, request: Request, idempotency_key: IdempotencyKey) -> RunView:
    return service_from(request).create_run(body, idempotency_key)


@router.get("/runs", response_model=list[RunView], tags=["runs"])
def list_runs(
    request: Request,
    project_id: str | None = None,
    state: WorkflowState | None = None,
) -> list[RunView]:
    return service_from(request).list_runs(project_id=project_id, state=state)


@router.get("/runs/{run_id}", response_model=RunView, tags=["runs"])
def get_run(run_id: str, request: Request) -> RunView:
    return service_from(request).get_run(run_id)


@router.post("/runs/{run_id}/diagnosis", response_model=RunDiagnosisView, tags=["runs"])
def diagnose_run(
    run_id: str, body: RunDiagnosisRequest, request: Request, idempotency_key: IdempotencyKey
) -> RunDiagnosisView:
    del idempotency_key
    return service_from(request).diagnose_run(run_id, body)


@router.post("/ml/datasets/inspect", response_model=MlTableInspectView, tags=["machine-learning"])
def inspect_ml_table(
    body: MlTableInspectRequest, request: Request, idempotency_key: IdempotencyKey
) -> MlTableInspectView:
    del idempotency_key
    return service_from(request).inspect_ml_table(body)


@router.post("/ml/templates", response_model=MlTemplateView, tags=["machine-learning"])
def create_ml_template(
    body: MlTemplateCreateRequest, request: Request, idempotency_key: IdempotencyKey
) -> MlTemplateView:
    del idempotency_key
    return service_from(request).create_ml_template(body)


@router.post("/roi/extractions/validate", response_model=RoiTableView, tags=["roi"])
def validate_roi_table(
    body: RoiTableCreateRequest, request: Request, idempotency_key: IdempotencyKey
) -> RoiTableView:
    del idempotency_key
    return service_from(request).validate_roi_table(body)


@router.post(
    "/cluster-localizations", response_model=ClusterLocalizationView, tags=["localization"]
)
def localize_clusters(
    body: ClusterLocalizationRequest, request: Request, idempotency_key: IdempotencyKey
) -> ClusterLocalizationView:
    del idempotency_key
    return service_from(request).localize_clusters(body)


@router.post("/agent/rsfmri/questions", response_model=RsFmriAnswerView, tags=["agent"])
def answer_rsfmri_question(
    body: RsFmriQuestionRequest, request: Request, idempotency_key: IdempotencyKey
) -> RsFmriAnswerView:
    del idempotency_key
    return service_from(request).answer_rsfmri_question(body)


@router.post("/organization/previews", response_model=OrganizationPreviewView, tags=["datasets"])
def organization_preview(
    body: OrganizationPreviewRequest, request: Request, idempotency_key: IdempotencyKey
) -> OrganizationPreviewView:
    del idempotency_key
    return service_from(request).organization_preview(body)


@router.post("/runs/{run_id}/cancel", response_model=RunView, tags=["runs"])
def cancel_run(
    run_id: str,
    body: RunAction,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> RunView:
    return service_from(request).cancel_run(run_id, body, idempotency_key)


@router.post("/runs/{run_id}/retry", response_model=RunView, tags=["runs"])
def retry_run(
    run_id: str,
    body: RunAction,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> RunView:
    return service_from(request).retry_run(run_id, body, idempotency_key)


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
    return service_from(request).create_qc_review(body, idempotency_key)


@router.get("/qc-reviews/{review_revision_id}", response_model=QcReviewView, tags=["qc"])
def get_qc_review(review_revision_id: str, request: Request) -> QcReviewView:
    return service_from(request).get_qc_review(review_revision_id)


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
    return service_from(request).approve_qc_review(review_revision_id, body, idempotency_key)


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactView], tags=["runs"])
def list_artifacts(run_id: str, request: Request) -> list[ArtifactView]:
    return service_from(request).list_artifacts(run_id)


@router.get("/artifacts/{artifact_id}", response_model=ArtifactView, tags=["artifacts"])
def get_artifact(artifact_id: str, request: Request) -> ArtifactView:
    return service_from(request).get_artifact(artifact_id)


@router.get("/runs/{run_id}/events", tags=["runs"])
async def stream_run_events(
    run_id: str,
    request: Request,
    after_event_id: int = Query(default=0, ge=0),
    once: bool = Query(default=False),
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    service = service_from(request)
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


@router.post(
    "/model-profiles",
    response_model=ModelProfileView,
    status_code=status.HTTP_201_CREATED,
    tags=["agent"],
)
def create_model_profile(
    body: ModelProfileCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ModelProfileView:
    return service_from(request).create_model_profile(body, idempotency_key)


@router.get("/model-profiles", response_model=list[ModelProfileView], tags=["agent"])
def list_model_profiles(request: Request) -> list[ModelProfileView]:
    return service_from(request).list_model_profiles()


@router.get("/model-profiles/{profile_id}", response_model=ModelProfileView, tags=["agent"])
def get_model_profile(profile_id: str, request: Request) -> ModelProfileView:
    return service_from(request).get_model_profile(profile_id)


@router.delete(
    "/model-profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["agent"],
)
def delete_model_profile(profile_id: str, request: Request) -> None:
    service_from(request).delete_model_profile(profile_id)


@router.post("/providers/models", response_model=ModelListView, tags=["agent"])
async def list_provider_models(
    body: ModelListRequest,
    request: Request,
) -> ModelListView:
    return await service_from(request).list_provider_models(body)


@router.post("/providers/test", response_model=ProviderTestView, tags=["agent"])
async def test_provider(
    body: ProviderTestRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ProviderTestView:
    return await service_from(request).test_provider(body, idempotency_key)


@router.post(
    "/agent/tasks",
    response_model=AgentTaskView,
    status_code=status.HTTP_201_CREATED,
    tags=["agent"],
)
async def create_agent_task(
    body: AgentTaskCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> AgentTaskView:
    return await service_from(request).create_agent_task(body, idempotency_key)


@router.get("/agent/tasks/{task_id}", response_model=AgentTaskView, tags=["agent"])
def get_agent_task(task_id: str, request: Request) -> AgentTaskView:
    return service_from(request).get_agent_task(task_id)
