"""HTTP endpoints for the literature capability."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, File, Request, UploadFile, status
from fastapi.responses import FileResponse

from neuroagent.api.routers.common import capability_from
from neuroagent.literature.models import PaperIngestResult

router = APIRouter()


@router.post(
    "/literature/papers",
    response_model=PaperIngestResult,
    status_code=status.HTTP_201_CREATED,
    tags=["literature"],
)
async def ingest_literature_paper(
    request: Request,
    file: Annotated[UploadFile, File(description="Scientific paper PDF")],
) -> PaperIngestResult:
    literature = capability_from(request, "literature")
    content = await file.read(literature.max_pdf_bytes + 1)
    await file.close()
    return await asyncio.to_thread(literature.ingest, filename=file.filename or "", content=content)


@router.post(
    "/literature/papers/{paper_id}/index", response_model=PaperIngestResult, tags=["literature"]
)
def index_literature_paper(paper_id: str, request: Request) -> PaperIngestResult:
    return capability_from(request, "literature").index(paper_id)


@router.get(
    "/literature/papers/{paper_id}/source", response_class=FileResponse, tags=["literature"]
)
def get_literature_source(paper_id: str, request: Request) -> FileResponse:
    return FileResponse(
        capability_from(request, "literature").source(paper_id), media_type="application/pdf"
    )


@router.get(
    "/literature/papers/{paper_id}",
    response_model=PaperIngestResult,
    tags=["literature"],
)
def get_literature_paper(paper_id: str, request: Request) -> PaperIngestResult:
    return capability_from(request, "literature").get(paper_id)


@router.get(
    "/literature/papers",
    response_model=list[PaperIngestResult],
    tags=["literature"],
)
def list_literature_papers(request: Request) -> list[PaperIngestResult]:
    return capability_from(request, "literature").list()
