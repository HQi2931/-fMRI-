"""HTTP endpoints for the agent capability."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, status
from fastapi.responses import StreamingResponse

from neuroagent.api.conversation_stream import conversation_stream_response
from neuroagent.api.responses.agent import (
    AgentTaskView,
    ModelListView,
    ModelProfileView,
    ProviderTestView,
    RsFmriAnswerView,
)
from neuroagent.api.responses.automation import (
    StepPlanReviewDetailView,
    StepPlanReviewListView,
)
from neuroagent.api.responses.conversations import (
    CompositeGoalProgressView,
    ConversationContextView,
    ConversationTurnView,
    ConversationView,
    MemoryView,
)
from neuroagent.api.routers.common import IdempotencyKey, capability_from
from neuroagent.api.schemas.agent import (
    AgentTaskCreate,
    ModelListRequest,
    ModelProfileCreate,
    ProviderTestRequest,
    RsFmriQuestionRequest,
)
from neuroagent.api.schemas.automation import (
    StepPlanApproveCreate,
    StepPlanApproveView,
    StepRunAuthorizationCreate,
    StepRunAuthorizationView,
    StepRunStartCreate,
    StepRunStartView,
)
from neuroagent.api.schemas.conversations import (
    CompositeGoalSelection,
    ConversationCreate,
    ConversationMode,
    ConversationTurnCreate,
    MemoryCreate,
    MemoryUpdate,
    WorkAgentSelection,
    WorkConfirmationCreate,
    WorkDraftUpdate,
    WorkReviewCreate,
)
from neuroagent.api.schemas.mapping import to_application_message, to_http_response
from neuroagent.application.contracts.agent import AgentTaskCreate as AppAgentTaskCreate
from neuroagent.application.contracts.agent import ModelListRequest as AppModelListRequest
from neuroagent.application.contracts.agent import ModelProfileCreate as AppModelProfileCreate
from neuroagent.application.contracts.agent import ProviderTestRequest as AppProviderTestRequest
from neuroagent.application.contracts.agent import RsFmriQuestionRequest as AppRsFmriQuestionRequest
from neuroagent.application.contracts.automation import (
    StepPlanApproveCreate as AppStepPlanApproveCreate,
)
from neuroagent.application.contracts.automation import (
    StepRunAuthorizationCreate as AppStepRunAuthorizationCreate,
)
from neuroagent.application.contracts.automation import (
    StepRunStartCreate as AppStepRunStartCreate,
)
from neuroagent.application.contracts.conversations import (
    CompositeGoalSelection as AppCompositeGoalSelection,
)
from neuroagent.application.contracts.conversations import (
    ConversationCreate as AppConversationCreate,
)
from neuroagent.application.contracts.conversations import (
    ConversationTurnCreate as AppConversationTurnCreate,
)
from neuroagent.application.contracts.conversations import MemoryCreate as AppMemoryCreate
from neuroagent.application.contracts.conversations import MemoryUpdate as AppMemoryUpdate
from neuroagent.application.contracts.conversations import (
    WorkAgentSelection as AppWorkAgentSelection,
)
from neuroagent.application.contracts.conversations import (
    WorkConfirmationCreate as AppWorkConfirmationCreate,
)
from neuroagent.application.contracts.conversations import WorkDraftUpdate as AppWorkDraftUpdate
from neuroagent.application.contracts.conversations import WorkReviewCreate as AppWorkReviewCreate

router = APIRouter()


@router.post(
    "/conversations",
    response_model=ConversationView,
    status_code=status.HTTP_201_CREATED,
    tags=["agent"],
)
def create_conversation(
    body: ConversationCreate, request: Request, idempotency_key: IdempotencyKey
) -> ConversationView:
    return cast(
        ConversationView,
        to_http_response(
            capability_from(request, "conversations").create_conversation(
                to_application_message(body, AppConversationCreate), idempotency_key
            ),
            ConversationView,
        ),
    )


@router.get("/conversations", response_model=list[ConversationView], tags=["agent"])
def list_conversations(
    request: Request, mode: ConversationMode | None = None
) -> list[ConversationView]:
    return cast(
        list[ConversationView],
        to_http_response(
            capability_from(request, "conversations").list_conversations(mode),
            list[ConversationView],
        ),
    )


@router.post("/conversations/{conversation_id}/context/index", tags=["agent"])
async def index_conversation_memories(conversation_id: str, request: Request) -> dict[str, str]:
    from neuroagent.memory.embeddings import MemoryEmbeddingError
    from neuroagent.shared.errors import ApplicationError

    try:
        return await capability_from(request, "semantic_memory").index(conversation_id)
    except MemoryEmbeddingError as exc:
        raise ApplicationError(
            str(exc), "记忆索引失败,请检查服务配置后重试。", status_code=503
        ) from exc


@router.get("/conversations/{conversation_id}", response_model=ConversationView, tags=["agent"])
def get_conversation(conversation_id: str, request: Request) -> ConversationView:
    return cast(
        ConversationView,
        to_http_response(
            capability_from(request, "conversations").get_conversation(conversation_id),
            ConversationView,
        ),
    )


@router.get(
    "/conversations/{conversation_id}/composite-goals",
    response_model=list[CompositeGoalProgressView],
    tags=["agent"],
)
def list_composite_goal_progress(
    conversation_id: str, request: Request
) -> list[CompositeGoalProgressView]:
    progress = capability_from(request, "conversations").automation.list_composite_goal_progress(
        conversation_id
    )
    return cast(
        list[CompositeGoalProgressView],
        to_http_response(
            [
                {
                    **item.goal.model_dump(mode="python"),
                    **item.model_dump(mode="python", exclude={"goal"}),
                }
                for item in progress
            ],
            list[CompositeGoalProgressView],
        ),
    )


@router.get(
    "/conversations/{conversation_id}/step-plan-reviews",
    response_model=StepPlanReviewListView,
    tags=["agent"],
)
def list_step_plan_reviews(conversation_id: str, request: Request) -> StepPlanReviewListView:
    reviews = capability_from(request, "conversations").automation.list_step_plan_reviews(
        conversation_id
    )
    return cast(
        StepPlanReviewListView,
        to_http_response(reviews, StepPlanReviewListView),
    )


@router.get(
    "/conversations/{conversation_id}/work-tasks/{task_id}/steps/{step_id}/review",
    response_model=StepPlanReviewDetailView,
    tags=["agent"],
)
def get_step_plan_review(
    conversation_id: str, task_id: str, step_id: str, request: Request
) -> StepPlanReviewDetailView:
    review = capability_from(request, "conversations").automation.get_step_plan_review(
        conversation_id, task_id, step_id
    )
    return cast(
        StepPlanReviewDetailView,
        to_http_response(review, StepPlanReviewDetailView),
    )


@router.post(
    "/conversations/{conversation_id}/work-tasks/{task_id}/steps/{step_id}/approve",
    response_model=StepPlanApproveView,
    status_code=status.HTTP_201_CREATED,
    tags=["agent", "plans"],
)
def approve_step_plan(
    conversation_id: str,
    task_id: str,
    step_id: str,
    body: StepPlanApproveCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> StepPlanApproveView:
    services = capability_from(request, "conversations")
    result = services.automation.approve_step_plan(
        conversation_id,
        task_id,
        step_id,
        to_application_message(body, AppStepPlanApproveCreate),
        idempotency_key=idempotency_key,
        approve_plan=capability_from(request, "plans").approve_plan,
    )
    return cast(
        StepPlanApproveView,
        to_http_response(
            {
                **result.model_dump(mode="python", exclude={"approval"}),
                "approval": result.approval.model_dump(mode="python"),
            },
            StepPlanApproveView,
        ),
    )


@router.post(
    "/conversations/{conversation_id}/work-tasks/{task_id}/steps/{step_id}/run-authorizations",
    response_model=StepRunAuthorizationView,
    status_code=status.HTTP_201_CREATED,
    tags=["agent", "runs"],
)
def create_step_run_authorization(
    conversation_id: str,
    task_id: str,
    step_id: str,
    body: StepRunAuthorizationCreate,
    request: Request,
) -> StepRunAuthorizationView:
    result = capability_from(request, "conversations").automation.create_step_run_authorization(
        conversation_id,
        task_id,
        step_id,
        to_application_message(body, AppStepRunAuthorizationCreate),
    )
    return cast(
        StepRunAuthorizationView,
        to_http_response(
            {
                "conversation_id": result.conversation_id,
                "project_id": result.project_id,
                "task_id": result.task_id,
                "step_id": result.step_id,
                "plan_revision_id": result.plan_revision_id,
                "plan_hash": result.plan_hash,
                "confirmation_id": result.authorization.confirmation_id,
                "expires_at": result.authorization.expires_at,
            },
            StepRunAuthorizationView,
        ),
    )


@router.post(
    "/conversations/{conversation_id}/work-tasks/{task_id}/steps/{step_id}/run",
    response_model=StepRunStartView,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["agent", "runs"],
)
def start_step_run(
    conversation_id: str,
    task_id: str,
    step_id: str,
    body: StepRunStartCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> StepRunStartView:
    result = capability_from(request, "conversations").automation.start_step_run(
        conversation_id,
        task_id,
        step_id,
        to_application_message(body, AppStepRunStartCreate),
        idempotency_key=idempotency_key,
        create_run=capability_from(request, "runs").create_run,
        create_statistics_run=capability_from(request, "runs").create_statistics_run,
    )
    return cast(StepRunStartView, to_http_response(result, StepRunStartView))


@router.post(
    "/conversations/{conversation_id}/analysis-outbox/{intent_task_id}/retry",
    response_model=ConversationView,
    tags=["agent"],
)
def retry_analysis_outbox(
    conversation_id: str,
    intent_task_id: str,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ConversationView:
    return cast(
        ConversationView,
        to_http_response(
            capability_from(request, "conversations").retry_analysis_outbox(
                conversation_id, intent_task_id, idempotency_key
            ),
            ConversationView,
        ),
    )


@router.post(
    "/conversations/{conversation_id}/statistical-result-interpretations/{task_id}/retry",
    response_model=ConversationView,
    tags=["agent"],
)
def retry_statistical_result_interpretation(
    conversation_id: str,
    task_id: str,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ConversationView:
    return cast(
        ConversationView,
        to_http_response(
            capability_from(request, "conversations").retry_result_interpretation_outbox(
                conversation_id, task_id, idempotency_key
            ),
            ConversationView,
        ),
    )


@router.patch(
    "/conversations/{conversation_id}/work-agent", response_model=ConversationView, tags=["agent"]
)
def select_work_agent(
    conversation_id: str,
    body: WorkAgentSelection,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ConversationView:
    return cast(
        ConversationView,
        to_http_response(
            capability_from(request, "conversations").select_work_agent(
                conversation_id,
                to_application_message(body, AppWorkAgentSelection),
                idempotency_key,
            ),
            ConversationView,
        ),
    )


@router.post(
    "/conversations/{conversation_id}/composite-goals/{intent_task_id}/select",
    response_model=ConversationView,
    tags=["agent"],
)
def select_composite_goal(
    conversation_id: str,
    intent_task_id: str,
    body: CompositeGoalSelection,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ConversationView:
    return cast(
        ConversationView,
        to_http_response(
            capability_from(request, "conversations").select_composite_goal(
                conversation_id,
                intent_task_id,
                to_application_message(body, AppCompositeGoalSelection),
                idempotency_key,
            ),
            ConversationView,
        ),
    )


@router.get(
    "/conversations/{conversation_id}/context",
    response_model=ConversationContextView,
    tags=["agent"],
)
def get_conversation_context(conversation_id: str, request: Request) -> ConversationContextView:
    return cast(
        ConversationContextView,
        to_http_response(
            capability_from(request, "conversations").get_conversation_context(conversation_id),
            ConversationContextView,
        ),
    )


@router.post(
    "/conversations/{conversation_id}/context/memories",
    response_model=MemoryView,
    status_code=status.HTTP_201_CREATED,
    tags=["agent"],
)
def create_conversation_memory(
    conversation_id: str,
    body: MemoryCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MemoryView:
    return cast(
        MemoryView,
        to_http_response(
            capability_from(request, "conversations").create_conversation_memory(
                conversation_id, to_application_message(body, AppMemoryCreate), idempotency_key
            ),
            MemoryView,
        ),
    )


@router.patch(
    "/conversations/{conversation_id}/context/memories/{memory_id}",
    response_model=MemoryView,
    tags=["agent"],
)
def update_conversation_memory(
    conversation_id: str,
    memory_id: str,
    body: MemoryUpdate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MemoryView:
    return cast(
        MemoryView,
        to_http_response(
            capability_from(request, "conversations").update_conversation_memory(
                conversation_id,
                memory_id,
                to_application_message(body, AppMemoryUpdate),
                idempotency_key,
            ),
            MemoryView,
        ),
    )


@router.patch(
    "/conversations/{conversation_id}/work-draft",
    response_model=ConversationTurnView,
    tags=["agent"],
)
def update_work_draft(
    conversation_id: str, body: WorkDraftUpdate, request: Request, idempotency_key: IdempotencyKey
) -> ConversationTurnView:
    return cast(
        ConversationTurnView,
        to_http_response(
            capability_from(request, "conversations").update_work_draft(
                conversation_id, to_application_message(body, AppWorkDraftUpdate), idempotency_key
            ),
            ConversationTurnView,
        ),
    )


@router.post(
    "/conversations/{conversation_id}/work-confirmation",
    response_model=ConversationTurnView,
    tags=["agent"],
)
async def confirm_work_action(
    conversation_id: str,
    body: WorkConfirmationCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ConversationTurnView:
    return cast(
        ConversationTurnView,
        to_http_response(
            await capability_from(request, "conversations").confirm_work_action(
                conversation_id,
                to_application_message(body, AppWorkConfirmationCreate),
                idempotency_key,
            ),
            ConversationTurnView,
        ),
    )


@router.post(
    "/conversations/{conversation_id}/work-review",
    response_model=ConversationTurnView,
    tags=["agent"],
)
def prepare_work_review(
    conversation_id: str,
    body: WorkReviewCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ConversationTurnView:
    return cast(
        ConversationTurnView,
        to_http_response(
            capability_from(request, "conversations").prepare_work_review(
                conversation_id, to_application_message(body, AppWorkReviewCreate), idempotency_key
            ),
            ConversationTurnView,
        ),
    )


@router.post(
    "/conversations/{conversation_id}/turns",
    response_model=ConversationTurnView,
    status_code=status.HTTP_201_CREATED,
    tags=["agent"],
    responses={201: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def send_conversation_turn(
    conversation_id: str,
    body: ConversationTurnCreate,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ConversationTurnView | StreamingResponse:
    if body.stream:
        command = to_application_message(body, AppConversationTurnCreate)
        service = capability_from(request, "conversations")
        return cast(
            ConversationTurnView | StreamingResponse,
            conversation_stream_response(
                mode=service.get_conversation(conversation_id).mode.value,
                send_turn=service.send_conversation_turn,
                map_response=lambda result: cast(
                    ConversationTurnView,
                    to_http_response(result, ConversationTurnView),
                ),
                conversation_id=conversation_id,
                body=command,
                key=idempotency_key,
                request=request,
            ),
        )
    return cast(
        ConversationTurnView | StreamingResponse,
        to_http_response(
            await capability_from(request, "conversations").send_conversation_turn(
                conversation_id,
                to_application_message(body, AppConversationTurnCreate),
                idempotency_key,
            ),
            ConversationTurnView | StreamingResponse,
        ),
    )


@router.post("/agent/rsfmri/questions", response_model=RsFmriAnswerView, tags=["agent"])
def answer_rsfmri_question(
    body: RsFmriQuestionRequest, request: Request, idempotency_key: IdempotencyKey
) -> RsFmriAnswerView:
    del idempotency_key
    command = to_application_message(body, AppRsFmriQuestionRequest)
    return cast(
        RsFmriAnswerView,
        to_http_response(
            capability_from(request, "runs").answer_rsfmri_question(command), RsFmriAnswerView
        ),
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
    return cast(
        ModelProfileView,
        to_http_response(
            capability_from(request, "models").create_model_profile(
                to_application_message(body, AppModelProfileCreate), idempotency_key
            ),
            ModelProfileView,
        ),
    )


@router.get("/model-profiles", response_model=list[ModelProfileView], tags=["agent"])
def list_model_profiles(request: Request) -> list[ModelProfileView]:
    return cast(
        list[ModelProfileView],
        to_http_response(
            capability_from(request, "models").list_model_profiles(), list[ModelProfileView]
        ),
    )


@router.get("/model-profiles/{profile_id}", response_model=ModelProfileView, tags=["agent"])
def get_model_profile(profile_id: str, request: Request) -> ModelProfileView:
    return cast(
        ModelProfileView,
        to_http_response(
            capability_from(request, "models").get_model_profile(profile_id), ModelProfileView
        ),
    )


@router.delete(
    "/model-profiles/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["agent"],
)
def delete_model_profile(profile_id: str, request: Request) -> None:
    capability_from(request, "models").delete_model_profile(profile_id)


@router.post("/providers/models", response_model=ModelListView, tags=["agent"])
async def list_provider_models(
    body: ModelListRequest,
    request: Request,
) -> ModelListView:
    return cast(
        ModelListView,
        to_http_response(
            await capability_from(request, "models").list_provider_models(
                to_application_message(body, AppModelListRequest)
            ),
            ModelListView,
        ),
    )


@router.post("/providers/test", response_model=ProviderTestView, tags=["agent"])
async def test_provider(
    body: ProviderTestRequest,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> ProviderTestView:
    return cast(
        ProviderTestView,
        to_http_response(
            await capability_from(request, "models").test_provider(
                to_application_message(body, AppProviderTestRequest), idempotency_key
            ),
            ProviderTestView,
        ),
    )


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
    return cast(
        AgentTaskView,
        to_http_response(
            await capability_from(request, "models").create_agent_task(
                to_application_message(body, AppAgentTaskCreate), idempotency_key
            ),
            AgentTaskView,
        ),
    )


@router.get("/agent/tasks/{task_id}", response_model=AgentTaskView, tags=["agent"])
def get_agent_task(task_id: str, request: Request) -> AgentTaskView:
    return cast(
        AgentTaskView,
        to_http_response(capability_from(request, "models").get_agent_task(task_id), AgentTaskView),
    )
