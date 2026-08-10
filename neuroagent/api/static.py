"""Production static-file hosting with single-page-application fallback."""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class SpaStaticFiles(StaticFiles):
    """Serve built assets and fall back to ``index.html`` for browser routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or scope["method"] not in {"GET", "HEAD"}:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404 and scope["method"] in {"GET", "HEAD"}:
            return await super().get_response("index.html", scope)
        return response
