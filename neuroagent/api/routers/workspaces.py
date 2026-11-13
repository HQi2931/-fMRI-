"""HTTP endpoints for the workspaces capability."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, status

from neuroagent.api.responses.conversations import (
    WorkspaceChangeProposal,
    WorkspaceContentConsent,
    WorkspaceFileListingResponse,
    WorkspacePatchApplyView,
    WorkspacePatchUndoView,
)
from neuroagent.api.responses.projects import WorkspaceCheckView, WorkspacePickView
from neuroagent.api.routers.common import capability_from
from neuroagent.api.schemas.conversations import (
    WorkspaceContentConsentCreate,
    WorkspacePatchApplyRequest,
)
from neuroagent.api.schemas.mapping import to_application_message, to_http_response
from neuroagent.api.schemas.projects import WorkspaceCheckRequest
from neuroagent.application.contracts.conversations import (
    WorkspaceContentConsentCreate as AppWorkspaceContentConsentCreate,
)
from neuroagent.application.contracts.conversations import (
    WorkspacePatchApplyRequest as AppWorkspacePatchApplyRequest,
)
from neuroagent.application.contracts.projects import (
    WorkspaceCheckRequest as AppWorkspaceCheckRequest,
)

router = APIRouter()


@router.post("/workspaces/check", response_model=WorkspaceCheckView, tags=["workspaces"])
def check_workspace(body: WorkspaceCheckRequest, request: Request) -> WorkspaceCheckView:
    """Read-only workspace check used by the conversational workbench."""

    return cast(
        WorkspaceCheckView,
        to_http_response(
            capability_from(request, "projects").check_workspace(
                to_application_message(body, AppWorkspaceCheckRequest)
            ),
            WorkspaceCheckView,
        ),
    )


@router.post("/workspaces/pick", response_model=WorkspacePickView, tags=["workspaces"])
def pick_workspace(request: Request) -> WorkspacePickView:
    return cast(
        WorkspacePickView,
        to_http_response(
            capability_from(request, "environment").pick_workspace(), WorkspacePickView
        ),
    )


@router.get(
    "/conversations/{conversation_id}/workspace/consents",
    response_model=list[WorkspaceContentConsent],
    tags=["workspaces"],
)
def list_workspace_consents(
    conversation_id: str, request: Request
) -> list[WorkspaceContentConsent]:
    return cast(
        list[WorkspaceContentConsent],
        to_http_response(
            capability_from(request, "workspaces").list_workspace_content_consents(conversation_id),
            list[WorkspaceContentConsent],
        ),
    )


@router.post(
    "/conversations/{conversation_id}/workspace/consents",
    response_model=WorkspaceContentConsent,
    status_code=status.HTTP_201_CREATED,
    tags=["workspaces"],
)
async def create_workspace_consent(
    conversation_id: str, body: WorkspaceContentConsentCreate, request: Request
) -> WorkspaceContentConsent:
    return cast(
        WorkspaceContentConsent,
        to_http_response(
            await capability_from(request, "workspaces").create_workspace_content_consent(
                conversation_id, to_application_message(body, AppWorkspaceContentConsentCreate)
            ),
            WorkspaceContentConsent,
        ),
    )


@router.delete(
    "/conversations/{conversation_id}/workspace/consents/{consent_id}",
    response_model=WorkspaceContentConsent,
    tags=["workspaces"],
)
def revoke_workspace_consent(
    conversation_id: str, consent_id: str, request: Request
) -> WorkspaceContentConsent:
    return cast(
        WorkspaceContentConsent,
        to_http_response(
            capability_from(request, "workspaces").revoke_workspace_content_consent(
                conversation_id, consent_id
            ),
            WorkspaceContentConsent,
        ),
    )


@router.get(
    "/conversations/{conversation_id}/workspace/files",
    response_model=WorkspaceFileListingResponse,
    response_model_exclude_none=True,
    tags=["workspaces"],
)
def list_workspace_files(
    conversation_id: str, request: Request, directory: str = ""
) -> WorkspaceFileListingResponse:
    return cast(
        WorkspaceFileListingResponse,
        to_http_response(
            capability_from(request, "workspaces").list_workspace_files(conversation_id, directory),
            WorkspaceFileListingResponse,
        ),
    )


@router.get(
    "/conversations/{conversation_id}/workspace/proposals/{proposal_id}",
    response_model=WorkspaceChangeProposal,
    tags=["workspaces"],
)
def get_workspace_proposal(
    conversation_id: str, proposal_id: str, request: Request
) -> WorkspaceChangeProposal:
    return cast(
        WorkspaceChangeProposal,
        to_http_response(
            capability_from(request, "workspaces").get_workspace_proposal(
                conversation_id, proposal_id
            ),
            WorkspaceChangeProposal,
        ),
    )


@router.post(
    "/conversations/{conversation_id}/workspace/proposals/{proposal_id}/apply",
    response_model=WorkspacePatchApplyView,
    tags=["workspaces"],
)
def apply_workspace_proposal(
    conversation_id: str, proposal_id: str, body: WorkspacePatchApplyRequest, request: Request
) -> WorkspacePatchApplyView:
    return cast(
        WorkspacePatchApplyView,
        to_http_response(
            capability_from(request, "workspaces").apply_workspace_proposal(
                conversation_id,
                proposal_id,
                to_application_message(body, AppWorkspacePatchApplyRequest),
            ),
            WorkspacePatchApplyView,
        ),
    )


@router.post(
    "/conversations/{conversation_id}/workspace/proposals/{proposal_id}/undo",
    response_model=WorkspacePatchUndoView,
    tags=["workspaces"],
)
def undo_workspace_proposal(
    conversation_id: str, proposal_id: str, request: Request
) -> WorkspacePatchUndoView:
    return cast(
        WorkspacePatchUndoView,
        to_http_response(
            capability_from(request, "workspaces").undo_workspace_proposal(
                conversation_id, proposal_id
            ),
            WorkspacePatchUndoView,
        ),
    )
