"""HTTP endpoints for the artifacts capability."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from neuroagent.api.responses.runs import ArtifactView
from neuroagent.api.routers.common import capability_from
from neuroagent.api.schemas.mapping import to_http_response

router = APIRouter()


@router.get("/artifacts/{artifact_id}", response_model=ArtifactView, tags=["artifacts"])
def get_artifact(artifact_id: str, request: Request) -> ArtifactView:
    return cast(
        ArtifactView,
        to_http_response(capability_from(request, "runs").get_artifact(artifact_id), ArtifactView),
    )


@router.get("/artifacts/{artifact_id}/content", response_class=FileResponse, tags=["artifacts"])
def download_artifact(artifact_id: str, request: Request) -> FileResponse:
    path = capability_from(request, "runs").artifact_content_path(artifact_id)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@router.get("/artifacts/{artifact_id}/preview.png", response_class=Response, tags=["artifacts"])
def preview_artifact(artifact_id: str, request: Request) -> Response:
    return Response(
        capability_from(request, "runs").artifact_preview_png(artifact_id),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )
