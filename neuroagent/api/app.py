"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from neuroagent.api.routes import router
from neuroagent.api.static import SpaStaticFiles
from neuroagent.application.contracts import ErrorBody, ErrorResponse
from neuroagent.application.errors import ApplicationError
from neuroagent.application.services import NeuroAgentService
from neuroagent.application.settings import Settings
from neuroagent.bootstrap import build_service
from neuroagent.observability.tracing import bind_trace_id, reset_trace_id

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    service: NeuroAgentService | None = None,
) -> FastAPI:
    owned_service = service is None
    resolved_settings = settings or (
        service.settings if service is not None else Settings.from_env()
    )
    resolved_service = service or build_service(resolved_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if owned_service:
            resolved_service.close()

    app = FastAPI(
        title="rs-fMRI Agent API",
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.state.service = resolved_service
    shared_errors: dict[int | str, dict[str, Any]] = {
        code: {"model": ErrorResponse} for code in (400, 403, 404, 409, 422, 500)
    }
    app.include_router(router, prefix="/api/v1", responses=shared_errors)

    @app.middleware("http")
    async def trace_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        trace_id = request.headers.get("X-Trace-ID") or str(uuid4())
        request.state.trace_id = trace_id
        token = bind_trace_id(trace_id)
        try:
            response = await call_next(request)
            response.headers["X-Trace-ID"] = trace_id
            return response
        finally:
            reset_trace_id(token)

    @app.exception_handler(ApplicationError)
    async def handle_application_error(request: Request, exc: ApplicationError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", str(uuid4()))
        body = ErrorResponse(
            error=ErrorBody(
                code=exc.code,
                message=exc.message,
                details=exc.details,
                trace_id=trace_id,
            )
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", str(uuid4()))
        body = ErrorResponse(
            error=ErrorBody(
                code="request_validation_failed",
                message="请求参数未通过校验。",
                details={
                    "issues": [
                        {
                            "type": str(issue.get("type", "validation_error")),
                            "location": [str(part) for part in issue.get("loc", ())],
                            "message": str(issue.get("msg", "invalid request")),
                        }
                        for issue in exc.errors()
                    ]
                },
                trace_id=trace_id,
            )
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", str(uuid4()))
        code = "method_not_allowed" if exc.status_code == 405 else "http_error"
        message = "请求方法不受支持。" if exc.status_code == 405 else "HTTP 请求无法处理。"
        body = ErrorResponse(
            error=ErrorBody(
                code=code,
                message=message,
                details={"path": request.url.path},
                trace_id=trace_id,
            )
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", str(uuid4()))
        logger.error(
            "Unhandled API error type=%s trace_id=%s",
            type(exc).__name__,
            trace_id,
        )
        body = ErrorResponse(
            error=ErrorBody(
                code="internal_server_error",
                message="服务发生未预期错误, 请使用 trace ID 查看本地日志。",
                details={},
                trace_id=trace_id,
            )
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    @app.api_route(
        "/api/v1/{unmatched_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def api_not_found(request: Request, unmatched_path: str) -> JSONResponse:
        del unmatched_path
        trace_id = getattr(request.state, "trace_id", str(uuid4()))
        body = ErrorResponse(
            error=ErrorBody(
                code="not_found",
                message="API 端点不存在。",
                details={"path": request.url.path},
                trace_id=trace_id,
            )
        )
        return JSONResponse(status_code=404, content=body.model_dump(mode="json"))

    if resolved_settings.serve_frontend or resolved_settings.environment == "production":
        app.mount(
            "/",
            SpaStaticFiles(directory=resolved_settings.frontend_dist, html=True),
            name="frontend",
        )

    return app
